"""Opt-in native executable tests, isolated fake auth and loopback HTTP only."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

from tests.support import RESPONSES, project
from tests.test_llm import sample_packet
from storyforge.config import configuration
from storyforge.llm import Client, CallPaused, codex_cli
from storyforge.store import serialize


@unittest.skipUnless(os.environ.get('SFL_TEST_NATIVE_CLI')=='1', 'Opt in to the local native CLI integration check')
class NativeRetryTests(unittest.TestCase):
    def run_case(self, failures, status=503, *, stream=False, partial=False):
        requests=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(handler):
                handler.rfile.read(int(handler.headers.get('Content-Length','0')))
                requests.append(time.monotonic())
                failing=len(requests)<=failures
                if failing and not stream:
                    payload=json.dumps({'error':{'type':'server_error','message':'Synthetic local failure'}}).encode()
                    handler.send_response(status)
                    handler.send_header('Content-Type','application/json')
                else:
                    message={'id':'msg_synthetic','type':'message','role':'assistant','status':'completed',
                        'content':[{'type':'output_text','text':serialize(RESPONSES['art_director:ep01'])}]}
                    response={'id':'resp_synthetic','object':'response','status':'completed',
                        'output':[message],'usage':{'input_tokens':10,'output_tokens':20,'total_tokens':30}}
                    events=[{'type':'response.created','response':{'id':'resp_synthetic','status':'in_progress'}},
                        {'type':'response.output_item.added','output_index':0,'item':{**message,'status':'in_progress','content':[]}},
                        {'type':'response.output_item.done','output_index':0,'item':message},
                        {'type':'response.completed','response':response}]
                    if failing:
                        events=events[:-1] if partial else events[:1]
                    payload=''.join('data: '+json.dumps(event)+'\n\n' for event in events).encode()
                    handler.send_response(200)
                    handler.send_header('Content-Type','text/event-stream')
                handler.send_header('Content-Length',str(len(payload)))
                handler.end_headers()
                handler.wfile.write(payload)
            def log_message(self,*args):
                pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix='sfl-native-test-') as temporary:
                root=Path(temporary)
                home=root/'isolated-auth'
                home.mkdir()
                (home/'auth.json').write_text('{"OPENAI_API_KEY":"synthetic-loopback-only"}',encoding='utf-8')
                store=project(root/'project')
                config=configuration()
                config['models']['strong']['timeout_seconds']=20
                original=codex_cli.arguments
                def arguments(profile,folder):
                    return original(profile,folder)[:-1]+['-c',
                        f'model_providers.storyforge.base_url="http://127.0.0.1:{server.server_port}/v1"',
                        '-c','cli_auth_credentials_store="file"','-']
                env=dict(os.environ,CODEX_HOME=str(home))
                for key in ('CODEX_API_KEY','OPENAI_API_KEY','OPENAI_BASE_URL'):
                    env.pop(key,None)
                with patch.object(codex_cli,'arguments',side_effect=arguments),patch.object(codex_cli,'environment',return_value=env):
                    client=Client(store,config,codex_transport=codex_cli.run)
                    if failures<3 and status==503 and not stream:
                        value=client.call(sample_packet(),'strong','style','B1','ep01')
                        self.assertEqual(value,RESPONSES['art_director:ep01'])
                        client.call(sample_packet(),'strong','style','B1','ep01')
                        self.assertTrue(store.logs('calls')[-1]['cached'])
                    else:
                        with self.assertRaises(CallPaused):
                            client.call(sample_packet(),'strong','style','B1','ep01')
                    count=1 if status==401 or partial else 3
                    self.assertEqual(len(requests),count,store.logs('calls'))
                    self.assertEqual([r['attempt'] for r in store.logs('calls') if not r['cached']],list(range(1,count+1)))
                    if count==3:
                        self.assertGreaterEqual(requests[1]-requests[0],1)
                        self.assertGreaterEqual(requests[2]-requests[1],2)
                    self.assertEqual((home/'auth.json').read_text(encoding='utf-8'),'{"OPENAI_API_KEY":"synthetic-loopback-only"}')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_503_twice_then_success_uses_three_requests_and_cache(self):
        self.run_case(2)

    def test_persistent_503_stops_at_three_native_requests(self):
        self.run_case(99)

    def test_auth_failure_is_not_retried(self):
        self.run_case(99,401)

    def test_stream_disconnect_before_output_uses_three_requests(self):
        self.run_case(99,stream=True)

    def test_stream_disconnect_after_output_pauses_without_replay(self):
        self.run_case(99,stream=True,partial=True)
