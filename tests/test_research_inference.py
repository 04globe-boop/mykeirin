import json,os,sys,time,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'logger'))
from research_inference import TimedInference


def hang(connection,bundle):
    connection.recv()
    # Deliberately unresponsive compute-only child; parent must stop it.
    time.sleep(60)


def echo(connection,bundle):
    message=connection.recv();value=json.loads(Path(message['input_path']).read_text(encoding='utf-8'))
    connection.send({'predictions':{'echo':value}});connection.close()


class InferenceBudget(unittest.TestCase):
    def test_hung_compute_is_killed_and_other_candidate_can_continue(self):
        runner=TimedInference('unused',budget_seconds=.25,target=hang)
        cutoff=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat();started=time.monotonic()
        with self.assertRaises(TimeoutError):runner.request({'frozen_input':1},cutoff)
        self.assertLess(time.monotonic()-started,2.)
        self.assertIsNone(runner.process)
        healthy=TimedInference('unused',target=echo)
        try:self.assertEqual(healthy.request([.2,.8],cutoff),{'echo':[.2,.8]})
        finally:healthy.close()

    def test_expired_cutoff_starts_no_process(self):
        runner=TimedInference('unused',target=hang)
        with self.assertRaises(TimeoutError):runner.request({},(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
        self.assertIsNone(runner.process)


if __name__=='__main__':unittest.main()
