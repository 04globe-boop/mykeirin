"""Bound model computation without killing the collector or its ledger writer.

The child reads only frozen models and receives already captured input values.
All ledger and gzip writes stay in the parent; a timed-out child cannot leave a
partially written prediction stream. Frozen source/weights and IDs are preserved.
"""
from datetime import datetime,timezone
import json,multiprocessing,time,tempfile
from pathlib import Path


def _inference_child(connection,bundle):
    try:
        from research_runtime import bundle_module
        root=Path(bundle);manifest=json.loads((root/'bundle.json').read_text(encoding='utf-8'))
        module=bundle_module(bundle,'prospective');data=bundle_module(bundle,'data')
        models={}
        for name,item in manifest['models'].items():
            directory=root/item['path']
            if module.model_fingerprint(directory)!=item['fingerprint']:raise ValueError('Model fingerprint differs')
            models[name]=module.load_candidate(directory)
        while True:
            message=connection.recv()
            if message is None:break
            payload=json.loads(Path(message['input_path']).read_text(encoding='utf-8'))
            race=data.Race.from_dict(payload).validate()
            connection.send({'predictions':{name:model.predict(race).tolist() for name,model in models.items()}})
    except EOFError:pass
    except BaseException as error:
        try:connection.send({'error':type(error).__name__+': '+str(error)[:200]})
        except (OSError,EOFError):pass
    finally:connection.close()


class TimedInference:
    def __init__(self,bundle,*,budget_seconds=3.,target=_inference_child):
        if not 0<budget_seconds<=3.:raise ValueError('Inference budget must be in (0,3] seconds')
        self.bundle=str(bundle);self.budget=budget_seconds;self.target=target
        self.process=None;self.connection=None;self.cached_key=None;self.cached_predictions=None

    def request(self,payload,deadline):
        remaining=(datetime.fromisoformat(deadline)-datetime.now(timezone.utc)).total_seconds()-.05
        if remaining<=0:raise TimeoutError('Prediction cutoff already reached')
        started=time.monotonic();allowed=min(self.budget,remaining)
        if self.process is None:
            ctx=multiprocessing.get_context('spawn');parent,child=ctx.Pipe()
            self.process=ctx.Process(target=self.target,args=(child,self.bundle),daemon=True)
            self.connection=parent;self.process.start();child.close()
        try:
            # Only a short path crosses the pipe, so a child stuck in startup
            # cannot block a large payload send before the timeout is applied.
            with tempfile.TemporaryDirectory(prefix='keirin-inference-',dir=__import__('os').environ.get('KEIRIN_TEST_TMP')) as tmp:
                path=Path(tmp)/'input.json';path.write_text(json.dumps(payload,allow_nan=False),encoding='utf-8')
                self.connection.send({'input_path':str(path)})
                if not self.connection.poll(max(0.,allowed-(time.monotonic()-started))):
                    raise TimeoutError('Frozen model inference exceeded its wall-clock/cutoff budget')
                response=self.connection.recv()
            if 'error' in response:raise RuntimeError(response['error'])
            if time.monotonic()-started>allowed:raise TimeoutError('Inference reply arrived after budget')
            return response['predictions']
        except BaseException:
            self.close();raise

    def predict(self,name,race):
        import numpy as np
        payload=race.as_dict()
        # Same race/input is requested by every frozen component in one worker.
        # No output is reused across different input snapshots or races.
        key=json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False)
        if key!=self.cached_key:
            self.cached_predictions=self.request(payload,race.decision_at);self.cached_key=key
        return np.asarray(self.cached_predictions[name],float)

    def close(self):
        if self.process is not None:
            if self.process.is_alive():self.process.terminate()
            self.process.join(timeout=.05)
            if self.process.is_alive():self.process.kill();self.process.join(timeout=.05)
            self.process=None
        if self.connection is not None:self.connection.close();self.connection=None
        self.cached_key=None;self.cached_predictions=None


class ModelProxy:
    def __init__(self,name,model,runner):self.name=name;self.original=model;self.runner=runner
    def __getattr__(self,name):return getattr(self.original,name)
    def predict(self,race):return self.runner.predict(self.name,race)
