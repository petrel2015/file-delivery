"""Verify actual Hermes skill and configured MCP without inference or providers."""
import argparse,hashlib,json,os,sys,tempfile
from pathlib import Path
from importlib.metadata import version
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--project',type=Path,default=Path(__file__).resolve().parents[1])
parser.add_argument('--hermes-source',type=Path,required=True)
args=parser.parse_args()
project=args.project.resolve()
sys.path.insert(0,str(args.hermes_source.resolve()))
os.chdir(project);os.environ['TERMINAL_CWD']=str(project)
from tools.skills_tool import skill_view
from tools.mcp_tool_discovery import discover_mcp_tools
from tools.mcp_tool_lifecycle import shutdown_mcp_servers
from tools.registry import registry
skill=json.loads(skill_view('file-delivery',preprocess=False))
assert 'error' not in skill,skill
assert Path(skill['skill_dir']).resolve()==project/'.agents/skills/file-delivery',skill.get('skill_dir')
body=(project/'.agents/skills/file-delivery/SKILL.md').read_text()
assert body.split('---',2)[2].strip() in skill['content']
report={'skill_loaded':True,'skill_sha256':hashlib.sha256(body.encode()).hexdigest(),'hermes_mcp_sdk_version':version('mcp'),'model_calls':0,'provider_calls':0}
try:
 names=discover_mcp_tools(allowed_mcp_names=['file-delivery'])
 def invoke(suffix,args):
  matching=[n for n in names if n.endswith('_'+suffix)]
  assert len(matching)==1,(suffix,names)
  result=registry.dispatch(matching[0],args)
  if isinstance(result,str):result=json.loads(result)
  if 'result' in result:
   result=result['result']
   if isinstance(result,str):result=json.loads(result)
  if isinstance(result.get('error'),str):
   try:result=json.loads(result['error'])
   except ValueError:pass
  # Hermes wraps MCP text/structured results depending on its version.
  if 'structuredContent' in result:result=result['structuredContent']
  if 'content' in result:
   for block in result['content']:
    if isinstance(block,dict) and block.get('type')=='text':result=json.loads(block['text']);break
  return result
 with tempfile.TemporaryDirectory(prefix='fd006-hermes-host-') as raw:
  base=Path(raw).resolve();root=base/'输入';root.mkdir();f=root/'报告.txt';f.write_text('真实 Hermes MCP 工具闭环\n')
  planned=invoke('plan',{'paths':[str(f)],'root':str(root)});assert planned['status']=='planned',planned
  assert planned['files'][0]['sha256']==hashlib.sha256(f.read_bytes()).hexdigest()
  args={'paths':[str(f)],'root':str(root),'state_dir':str(base/'state'),'store_dir':str(base/'objects'),'key':'host-check-1'}
  first=invoke('deliver_local',args);second=invoke('deliver_local',args)
  assert first['status']=='stored-local' and second['reused'] and first['task_id']==second['task_id'],(first,second)
  saved=invoke('status_local',{'state_dir':args['state_dir'],'key':args['key']});assert saved['task_id']==first['task_id']
  error=invoke('status_local',{'state_dir':args['state_dir'],'key':'missing'})
  assert error['error']['code']=='TASK_NOT_FOUND',error
  report.update(registered_host_tool_count=len(names),plan_hash_verified=True,local_delivery=True,duplicate_reused=True,status_verified=True,diagnostic_code=error['error']['code'])
finally:shutdown_mcp_servers(names={'file-delivery'})
print(json.dumps(report))
