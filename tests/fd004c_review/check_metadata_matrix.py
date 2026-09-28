"""AC2 repair completeness: every missing tuple combination and lawful new-task recovery."""
import argparse,itertools,json,sqlite3,sys
from pathlib import Path
from unittest import mock
parser=argparse.ArgumentParser();parser.add_argument('candidate_root',type=Path);args=parser.parse_args();root=args.candidate_root.resolve();sys.dont_write_bytecode=True
sys.path[:0]=[str(root/'src'),str(root/'tests/fd004c')]
import test_fd004c_remote as fixture
from file_delivery import remote
from file_delivery.errors import DeliveryError
fields=['provider_identity','retention_days','first_upload_at','retention_expires_at'];results=[]
for n in range(1,5):
 for missing in itertools.combinations(fields,n):
  t=fixture.LifecycleTests();t.setUp()
  try:
   t.deliver();before=t.store.count('delete')
   with sqlite3.connect(t.state/'remote.sqlite3') as db:db.execute('UPDATE tasks SET '+','.join(field+'=NULL' for field in missing))
   error=None
   for retry in range(2):
    try:t.deliver()
    except DeliveryError:pass
   try:t.revoke()
   except DeliveryError as exc:error=exc.code
   results.append({'case':'completed_missing_metadata','missing':list(missing),'passed':error=='STATE_INVALID' and t.store.count('delete')==before,'code':error,'additional_deletes':t.store.count('delete')-before})
  finally:t.doCleanups()
for state in ('pending','packaged'):
 t=fixture.LifecycleTests();t.setUp()
 try:
  if state=='pending':
   def checkpoint(stage):
    if stage=='after_pack':raise OSError('synthetic-before-packaged')
   try:t.deliver(checkpoint=checkpoint)
   except DeliveryError:pass
  else:
   original=remote._set_state
   def before_intent(conn,key,target,**kw):
    result=original(conn,key,target,**kw)
    if target==remote.STATE_PACKAGED:raise OSError('synthetic-after-packaged-before-intent')
    return result
   with mock.patch.object(remote,'_set_state',side_effect=before_intent):
    try:t.deliver()
    except DeliveryError:pass
  with sqlite3.connect(t.state/'remote.sqlite3') as db:row=db.execute('SELECT state,first_upload_at,retention_expires_at FROM tasks').fetchone()
  t.deliver();outcome=t.revoke()
  results.append({'case':'legitimate_'+state+'_recovery','before_state':row[0],'times_initially_null':row[1:] == (None,None),'status':outcome['status'],'passed':row==(state,None,None) and outcome['status']=='object-deleted'})
 finally:t.doCleanups()
# Repair-related crash: a rejected old tuple must not become fresh via transient packaged state.
t=fixture.LifecycleTests();t.setUp()
try:
 t.deliver()
 with sqlite3.connect(t.state/'remote.sqlite3') as db:db.execute('UPDATE tasks SET first_upload_at=NULL,retention_expires_at=NULL')
 original=remote._set_state;packaged_transitions=[]
 def stop_before_intent(conn,key,target,**kw):
  result=original(conn,key,target,**kw)
  if target==remote.STATE_PACKAGED:
   packaged_transitions.append(target)
   raise OSError('synthetic-after-packaged-commit')
  return result
 with mock.patch.object(remote,'_set_state',side_effect=stop_before_intent):
  try:t.deliver()
  except DeliveryError:pass
 before=t.store.count('delete');error=None
 try:t.deliver();t.revoke()
 except DeliveryError as exc:error=exc.code
 results.append({'case':'completed_missing_dates_interrupted_reentry','passed':error=='STATE_INVALID' and t.store.count('delete')==before and not packaged_transitions,'historical_packaged_transition_reached':bool(packaged_transitions),'code':error,'additional_deletes':t.store.count('delete')-before})
finally:t.doCleanups()
print(json.dumps({'tests_run':len(results),'passed':sum(r['passed'] for r in results),'results':results},indent=2));sys.exit(0 if all(r['passed'] for r in results) else 1)
