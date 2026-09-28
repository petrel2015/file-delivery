import sys,json,sqlite3,os,argparse
from pathlib import Path
parser=argparse.ArgumentParser();parser.add_argument('candidate_root',type=Path);args=parser.parse_args();candidate=args.candidate_root.resolve();sys.dont_write_bytecode=True
sys.path[:0]=[str(candidate/'src'),str(candidate/'tests/fd004b')]
from test_fd004b_remote import RemoteTests
from file_delivery.errors import DeliveryError
from file_delivery import remote,errors
results=[]
def case(name,fn):
 t=RemoteTests();t.setUp()
 try:
  try: result=fn(t);results.append({'case':name,'actual':result})
  except Exception as e:results.append({'case':name,'exception_type':type(e).__name__,'code':getattr(e,'code',None),'message':str(e)})
 finally:t.doCleanups()
def expiry(t):
 r=t.deliver();p=Path(r['handoff_file']);h=json.loads(p.read_text());h['expires_at']+=123456;p.write_text(json.dumps(h));again=t.deliver()
 return {'status':again['status'],'handoff_expiry_matches_result':h['expires_at']==again['expires_at']}
def crash_url(t):
 def fault(stage):
  if stage=='after_link':raise OSError('synthetic_checkpoint')
 try:t.deliver(checkpoint=fault)
 except DeliveryError:pass
 p=next((t.state/'remote-handoffs').glob('*.json'));h=json.loads(p.read_text());h['url']='https://attacker.example.test/injected';p.write_text(json.dumps(h));r=t.deliver()
 return {'status':r['status'],'tampered_url_adopted':json.loads(p.read_text())['url']==h['url']}
def status_permissions(t):
 t.deliver();t.state.chmod(0o755);return {'status':t.status()['status'],'mode':oct(t.state.stat().st_mode&0o777)}
def checkpoint(t):
 def fault(stage):raise RuntimeError('PRIVATE_SENTINEL_CHECKPOINT')
 return t.deliver(checkpoint=fault)
def symlink_dir(t):
 t.state.mkdir(mode=0o700);outside=t.base/'outside';outside.mkdir(mode=0o700);(t.state/'remote-handoffs').symlink_to(outside,target_is_directory=True);r=t.deliver()
 return {'status':r['status'],'handoff_written_outside_state':bool(list(outside.glob('*.json')))}
def code_secret(t):
 def fault():raise DeliveryError('PRIVATE_SENTINEL_CODE','hidden')
 t.store.ensure_private=fault
 try:t.deliver()
 except DeliveryError as e:
  return {'exception_type':type(e).__name__,'returned_code':e.code,'persisted_last_error':t.status().get('last_error')}
def status_parent_paths(t):
 t.deliver();observed=[]
 for name in ('remote-handoffs','remote-bundles'):
  directory=t.state/name;outside=t.base/('outside-'+name);directory.rename(outside);directory.symlink_to(outside,target_is_directory=True)
  try:
   value=t.status();observed.append({'directory':name,'rejected':False,'status':value['status']})
  except DeliveryError as exc:observed.append({'directory':name,'rejected':exc.code=='STATE_INVALID','code':exc.code})
  finally:directory.unlink();outside.rename(directory)
 return {'all_rejected':all(row['rejected'] for row in observed),'cases':observed}
for name,fn in [('handoff_expiry_tamper',expiry),('after_link_crash_url_tamper',crash_url),('status_public_state',status_permissions),('checkpoint_runtime_error',checkpoint),('handoff_parent_symlink',symlink_dir),('provider_untrusted_code',code_secret),('status_parent_paths',status_parent_paths)]:case(name,fn)
for result in results:
 name=result['case']
 if name in ('handoff_expiry_tamper','after_link_crash_url_tamper','status_public_state','handoff_parent_symlink'):
  passed=result.get('exception_type')=='DeliveryError' and result.get('code')=='STATE_INVALID'
 elif name=='status_parent_paths':
  passed=result.get('actual',{}).get('all_rejected') is True
 elif name=='checkpoint_runtime_error':
  passed=result.get('exception_type')=='DeliveryError' and 'PRIVATE_SENTINEL' not in json.dumps(result)
 else:
  actual=result.get('actual') or {}
  safe_codes={value for key,value in vars(errors).items() if key.isupper() and isinstance(value,str)}
  passed=(actual.get('exception_type')=='DeliveryError' and actual.get('returned_code') in safe_codes and actual.get('persisted_last_error') in safe_codes and 'PRIVATE_SENTINEL' not in json.dumps(result))
 result['passed']=passed
print(json.dumps({'tests_run':len(results),'passed':sum(r['passed'] for r in results),'results':results},indent=2))
sys.exit(0 if all(r['passed'] for r in results) else 1)
