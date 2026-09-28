"""FD004C independent semantic regression checks; all synthetic and offline."""
import argparse,json,sys,sqlite3
from pathlib import Path
parser=argparse.ArgumentParser();parser.add_argument('candidate_root',type=Path);args=parser.parse_args();root=args.candidate_root.resolve();sys.dont_write_bytecode=True
sys.path[:0]=[str(root/'src'),str(root/'tests/fd004c')]
import test_fd004c_remote as fixture
from file_delivery import remote
from file_delivery.errors import DeliveryError
results=[]
def run(name,fn):
 t=fixture.LifecycleTests();t.setUp()
 try:
  try:result=fn(t);results.append({'case':name,**result})
  except Exception as exc:results.append({'case':name,'passed':False,'unexpected_exception':type(exc).__name__,'code':getattr(exc,'code',None),'message':str(exc)})
 finally:t.doCleanups()
def legacy_reentry(t):
 t.deliver()
 with sqlite3.connect(t.state/'remote.sqlite3') as db:db.execute('UPDATE tasks SET provider_identity=NULL,retention_days=NULL,first_upload_at=NULL,retention_expires_at=NULL')
 t.deliver();before=t.store.count('delete')
 try:r=t.revoke();return {'passed':False,'observed':r['status'],'additional_deletes':t.store.count('delete')-before}
 except DeliveryError as exc:return {'passed':exc.code=='STATE_INVALID' and t.store.count('delete')==before,'code':exc.code}
def deleted_reappears(t):
 r=t.deliver();t.revoke();p=t.store.object_path(r['object_key']);p.write_bytes(b'do-not-delete-reappeared-object');before=t.store.count('delete')
 try:t.revoke()
 except DeliveryError:pass
 return {'passed':p.exists() and t.store.count('delete')==before,'additional_deletes':t.store.count('delete')-before,'replacement_survives':p.exists()}
def probe_exception_cleanup(t):
 t.deliver('one',retention_days=1,ttl_seconds=60);t.deliver('two',retention_days=1,ttl_seconds=60)
 count=[0]
 def probe(url):
  count[0]+=1
  if count[0]==1:raise RuntimeError('PRIVATE_SYNTHETIC_PROBE_MARKER')
  return {'status':'link-unknown','http_status':None}
 t.store.probe_link=probe
 try:
  r=t.cleanup(config_path=str(t.config),dry_run=False,now=2**40)
  states=[remote.status(t.state,key)['status'] for key in ('one','two')]
  return {'passed':states==['object-deleted','object-deleted'],'states':states,'status':r['status']}
 except Exception as exc:return {'passed':False,'exception':type(exc).__name__,'raw_marker_exposed':'PRIVATE_SYNTHETIC_PROBE_MARKER' in str(exc),'delete_count':t.store.count('delete')}
def session_auth(t):
 import requests
 from file_delivery.qiniu_store import QiniuStore
 class Adapter(requests.adapters.BaseAdapter):
  def __init__(self):self.seen=[]
  def send(self,request,**kw):
   self.seen.append(request);r=requests.Response();r.status_code=404;r._content=b'';r._content_consumed=True;r.request=request;r.url=request.url;return r
  def close(self):pass
 session=requests.Session();session.trust_env=False;session.auth=('synthetic-user','synthetic-auth-password');adapter=Adapter();session.mount('https://',adapter)
 try:
  r=QiniuStore.from_file(t.config,session=session).probe_link('https://files.example.com/object.zip?token=synthetic-signed-token')
  leaked=bool(adapter.seen and adapter.seen[0].headers.get('Authorization'))
  return {'passed':len(adapter.seen)==1 and not leaked,'authorization_on_prepared_request':leaked,'status':r['status']}
 finally:session.close()
def corrupted_task_owner(t):
 r=t.deliver();task='../unowned';key='file-delivery/'+task+'.zip'
 t.store.object_path(key).write_bytes(b'unowned-object')
 with sqlite3.connect(t.state/'remote.sqlite3') as db:db.execute('UPDATE tasks SET task_id=?,object_key=? WHERE key=?',(task,key,'one'))
 before=len(t.store.events())
 try:r=t.revoke();return {'passed':False,'observed':r['status'],'unowned_survives':t.store.object_path(key).exists()}
 except DeliveryError as exc:return {'passed':exc.code=='STATE_INVALID' and len(t.store.events())==before,'code':exc.code}
def previously_persisted_handoff_missing(t):
 r=t.deliver();Path(r['handoff_file']).unlink();before=len(t.store.events())
 try:r=t.revoke();return {'passed':False,'status':r['status'],'link_status':r['link_status'],'delete_count':t.store.count('delete')}
 except DeliveryError as exc:return {'passed':exc.code=='STATE_INVALID' and len(t.store.events())==before,'code':exc.code}
def partial_metadata_reentry(t):
 t.deliver()
 with sqlite3.connect(t.state/'remote.sqlite3') as db:db.execute('UPDATE tasks SET first_upload_at=NULL')
 t.deliver();before=t.store.count('delete')
 try:r=t.revoke();return {'passed':False,'observed':r['status'],'additional_deletes':t.store.count('delete')-before}
 except DeliveryError as exc:return {'passed':exc.code=='STATE_INVALID' and t.store.count('delete')==before,'code':exc.code}
def session_netrc(t):
 import requests
 from unittest import mock
 from file_delivery.qiniu_store import QiniuStore
 class Adapter(requests.adapters.BaseAdapter):
  def __init__(self):self.seen=[]
  def send(self,request,**kw):
   self.seen.append(request);r=requests.Response();r.status_code=404;r._content=b'';r._content_consumed=True;r.request=request;r.url=request.url;return r
  def close(self):pass
 session=requests.Session();session.trust_env=True;adapter=Adapter();session.mount('https://',adapter)
 try:
  with mock.patch('requests.sessions.get_netrc_auth',return_value=('synthetic-user','synthetic-password')):
   r=QiniuStore.from_file(t.config,session=session).probe_link('https://files.example.com/object.zip')
  leaked=bool(adapter.seen and adapter.seen[0].headers.get('Authorization'))
  return {'passed':len(adapter.seen)==1 and not leaked,'authorization_on_prepared_request':leaked,'status':r['status']}
 finally:session.close()
for name,fn in [('legacy_metadata_reentry',legacy_reentry),('already_deleted_reappearing_object',deleted_reappears),('probe_exception_cleanup_continuation',probe_exception_cleanup),('session_auth_no_credentials',session_auth),('corrupt_task_id_ownership',corrupted_task_owner),('previously_persisted_handoff_missing',previously_persisted_handoff_missing),('partial_retention_metadata_reentry',partial_metadata_reentry),('session_netrc_no_credentials',session_netrc)]:run(name,fn)
print(json.dumps({'tests_run':len(results),'passed':sum(r['passed'] for r in results),'results':results},indent=2));sys.exit(0 if all(r['passed'] for r in results) else 1)
