import hashlib,json,os,sys,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'logger'))
import research_assets


class ArtifactIntegrity(unittest.TestCase):
    def archive(self,root,member):
        dest=root/'runtime';dest.mkdir();archive=dest/'model.zip'
        with zipfile.ZipFile(archive,'w') as z:z.writestr(member,'{}')
        ref=root/'reference.json';ref.write_text(json.dumps({'tag':'fixed','file':'model.zip','sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}))
        return ref,dest

    def test_pinned_bundle_extracts(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            ref,dest=self.archive(Path(tmp),'bundle.json')
            with patch.object(research_assets,'download',return_value=True):
                result=research_assets.install_bundle('04globe-boop/mykeirin2',ref,dest)
            self.assertTrue((result/'bundle.json').exists())

    def test_changed_archive_never_extracts(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            ref,dest=self.archive(Path(tmp),'bundle.json')
            with (dest/'model.zip').open('ab') as f:f.write(b'changed')
            with patch.object(research_assets,'download',return_value=True):
                with self.assertRaises(ValueError):research_assets.install_bundle('04globe-boop/mykeirin2',ref,dest)
            self.assertFalse((dest/'bundle').exists())

    def test_archive_paths_cannot_escape(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            ref,dest=self.archive(Path(tmp),'../outside.json')
            with patch.object(research_assets,'download',return_value=True):
                with self.assertRaises(ValueError):research_assets.install_bundle('04globe-boop/mykeirin2',ref,dest)
            self.assertFalse((dest/'outside.json').exists())


if __name__=='__main__':unittest.main()
