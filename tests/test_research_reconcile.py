from copy import deepcopy
import json,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'logger'))
from research_reconcile import reconcile_day

def event():
    return {'workflow_run':{'name':'nk-results','event':'schedule','status':'completed','conclusion':'success','head_branch':'main',
        'head_repository':{'full_name':'04globe-boop/mykeirin'},'run_started_at':'2026-10-23T21:47:00Z'}}

class ScheduledResultDependency(unittest.TestCase):
    def test_after_midnight_utc_source_run_reconciles_its_previous_japanese_day(self):
        self.assertEqual(reconcile_day(event(),'04globe-boop/mykeirin'),'2026-10-23')
    def test_source_failure_also_rechecks_saved_sources_and_exposes_missingness(self):
        value=event();value['workflow_run']['conclusion']='failure'
        self.assertEqual(reconcile_day(value,'04globe-boop/mykeirin'),'2026-10-23')
    def test_manual_historical_collection_is_not_guessed_as_yesterday(self):
        value=event();value['workflow_run']['event']='workflow_dispatch'
        self.assertIsNone(reconcile_day(value,'04globe-boop/mykeirin'))
    def test_foreign_repository_branch_and_workflow_cannot_dispatch_writes(self):
        for key,value in [('head_repository',{'full_name':'visitor/fork'}),('head_branch','feature'),('name','untrusted'),('status','in_progress')]:
            source=event();source['workflow_run'][key]=value
            with self.subTest(key=key):self.assertIsNone(reconcile_day(source,'04globe-boop/mykeirin'))
        self.assertIsNone(reconcile_day(event(),'visitor/fork'))
    def test_retries_use_source_run_date_instead_of_current_wall_clock(self):
        source=event();source['workflow_run']['run_started_at']='2026-10-20T21:47:00+00:00'
        self.assertEqual(reconcile_day(source,'04globe-boop/mykeirin'),'2026-10-20')
    def test_naive_start_time_is_not_accepted_as_a_known_date(self):
        source=event();source['workflow_run']['run_started_at']='2026-10-23T21:47:00'
        with self.assertRaises(ValueError):reconcile_day(source,'04globe-boop/mykeirin')

if __name__=='__main__':unittest.main()
