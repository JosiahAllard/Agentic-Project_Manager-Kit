"""Agent-neutral, bounded MCP bridge interface over the common core.

The native frontend supplies caller metadata outside tool arguments. An owned
stdio or verified private IPC connection must establish its provenance before
using this component. No owner writes, identity overrides or proof arguments
are exposed. This module installs no plugin and starts no production service.
"""
from dataclasses import asdict
import hashlib
import json

from kitlib.ident import identifier
from .callers import CallProofs
from .dispatch import CoreBroker
from .jsonwire import _check_structure
from .messages import DeliveryError, MessageIntent, text_bytes
from .pairing import PairingError

MAX_RPC_BYTES=1024*1024
PROTOCOLS=('2025-06-18','2025-03-26','2024-11-05')


def _schema(properties,required=()):
    return {'type':'object','properties':{key:{'type':kind} for key,kind in properties.items()},
        'required':list(required),'additionalProperties':False}


OP={'operation_id':'string'}
TOOLS={
    'bridge_connections':('List only chats this caller may send to; stale native bindings revoke their connections.',_schema({}),False),
    'bridge_send':('Durably queue a message for an owner-selected chat. A queued receipt is not delivery.',
        _schema({'operation_id':'string','recipient':'string','text':'string','expires_at':'number'},
            ('operation_id','recipient','text')),False),
    'bridge_receipt':('Read this caller\'s message receipt.',_schema(OP,OP),True),
    'bridge_read_reply':('Read a correlated reply with the current reverse connection selected; stale bindings revoke connections.',_schema(OP,OP),False),
    'bridge_reply':('Reply to an exact operation addressed to this native chat.',
        _schema({**OP,'text':'string'},('operation_id','text')),False),
    'bridge_events':('Read this caller\'s message events.',
        _schema({**OP,'after':'integer','limit':'integer'},OP),True),
    'bridge_poll_native':('Refresh and persist this caller\'s existing receipt without resending the message.',_schema(OP,OP),False),
}


def _error(identity,code,message):
    return {'jsonrpc':'2.0','id':identity,'error':{'code':code,'message':message}}


def _args(name,args):
    schema=TOOLS[name][1]
    if type(args) is not dict or set(args)-set(schema['properties']) or set(schema['required'])-set(args):
        raise DeliveryError('invalid-tool-arguments')
    types={'string':(str,),'integer':(int,),'number':(int,float)}
    for key,value in args.items():
        if type(value) not in types[schema['properties'][key]['type']]:
            raise DeliveryError('invalid-tool-arguments')
    return args


class Server:
    def __init__(self,core,calls):
        if type(core) is not CoreBroker or type(calls) is not CallProofs:
            raise DeliveryError('invalid-mcp-components')
        self.core,self.calls=core,calls
        self._initializing=self._initialized=False

    def _call(self,name,args,metadata):
        args=_args(name,args)
        if name=='bridge_connections':
            authority=self.calls.observe(metadata)
            peers=[]
            for binding in self.core.pairings.registered():
                try: self.core.pairings.require_selected(authority.sender,binding)
                except PairingError: continue
                peers.append({'reference':binding.reference,'chat_id':binding.identity.session_id,
                    'runtime':binding.identity.adapter_id})
            return {'chats':peers}
        operation=identifier(args['operation_id'])
        if name=='bridge_send':
            recipient=next((binding for binding in self.core.pairings.registered()
                if binding.reference==args['recipient']),None)
            if recipient is None: raise DeliveryError('recipient-not-observed')
            intent=MessageIntent(operation,recipient,hashlib.sha256(text_bytes(args['text'])).hexdigest(),
                args.get('expires_at'))
            proof=self.calls.issue(intent,metadata)
            return asdict(self.core.admit(operation,recipient,args['text'],proof,expires_at=args.get('expires_at')))
        authority=self.calls.observe(metadata)
        try:
            message=self.core.outbox.message(operation)
        except DeliveryError:
            raise DeliveryError('operation-not-available') from None
        expected=message.recipient if name=='bridge_reply' else message.sender
        if authority.sender!=expected:
            raise DeliveryError('operation-not-available')
        proof=self.calls.issue(message.intent,metadata)
        if name=='bridge_receipt': return asdict(self.core.receipt(operation,proof))
        if name=='bridge_read_reply': return {'reply':self.core.reply(operation,proof)}
        if name=='bridge_reply': return asdict(self.core.reply_to(operation,args['text'],proof))
        if name=='bridge_poll_native': return asdict(self.core.poll_native(operation,proof))
        if name=='bridge_events':
            return {'events':[asdict(event) for event in self.core.events(operation,proof,
                after=args.get('after',0),limit=args.get('limit',32))]}
        raise DeliveryError('unknown-tool')

    def handle(self,message):
        if (type(message) is not dict or message.get('jsonrpc')!='2.0'
                or type(message.get('method')) is not str
                or set(message)-{'jsonrpc','id','method','params'}
                or 'id' in message and type(message['id']) not in (str,int)):
            return _error(None,-32600,'invalid request')
        method,identity=message['method'],message.get('id')
        params=message.get('params',{})
        if type(params) is not dict: return _error(identity,-32602,'invalid params')
        if 'id' not in message:
            if method=='notifications/initialized' and self._initializing:
                self._initialized=True
            return None  # Notifications cannot perform a write or call a tool.
        if method=='initialize':
            if self._initializing: return _error(identity,-32600,'already initialized')
            want=params.get('protocolVersion')
            self._initializing=True
            result={'protocolVersion':want if want in PROTOCOLS else PROTOCOLS[0],
                'serverInfo':{'name':'pm-kit-agent-bridge','version':'0.1.0'},
                'capabilities':{'tools':{'listChanged':False}},
                'instructions':'Only owner-selected connections may communicate. A queued or unknown receipt is not confirmed delivery. Use bridge_reply for correlated replies.'}
        elif method=='ping': result={}
        elif not self._initialized: return _error(identity,-32000,'not initialized')
        elif method=='tools/list':
            result={'tools':[{'name':name,'description':description,'inputSchema':schema,
                'annotations':{'readOnlyHint':read_only,'destructiveHint':False}}
                for name,(description,schema,read_only) in TOOLS.items()]}
        elif method=='tools/call':
            name=params.get('name')
            if type(name) is not str or name not in TOOLS:
                return _error(identity,-32602,'unknown tool')
            if set(params)-{'name','arguments','_meta'}:
                return _error(identity,-32602,'invalid params')
            try:
                payload=self._call(name,params.get('arguments',{}),params.get('_meta',{}))
                failed=False
            except (DeliveryError,PairingError) as error:
                payload={'error':{'code':error.code}}
                failed=True
            except Exception:
                payload={'error':{'code':'bridge-call-unavailable'}}
                failed=True
            result={'content':[{'type':'text','text':json.dumps(payload,ensure_ascii=False,separators=(',',':'))}],
                'structuredContent':payload,'isError':failed}
        else: return _error(identity,-32601,'method not found')
        return {'jsonrpc':'2.0','id':identity,'result':result}


def _pairs(values):
    result={}
    for key,value in values:
        if key in result: raise ValueError('duplicate key')
        result[key]=value
    return result


def serve(server,input_stream,output_stream):
    """Bounded JSON-RPC stdio. Oversize input ends the connection before dispatch."""
    while True:
        raw=input_stream.readline(MAX_RPC_BYTES+1)
        if not raw: return
        oversized=len(raw)>MAX_RPC_BYTES
        try:
            if oversized or not raw.endswith(b'\n'): raise ValueError('invalid frame')
            def invalid(value): raise ValueError('nonfinite number')
            message=json.loads(raw.decode('utf-8'),object_pairs_hook=_pairs,parse_constant=invalid)
            _check_structure(message,MAX_RPC_BYTES)
        except Exception:
            response=_error(None,-32700,'invalid JSON frame')
        else:
            try: response=server.handle(message)
            except Exception: response=_error(message.get('id'),-32603,'bridge request unavailable')
        if response is not None:
            try:
                _check_structure(response,MAX_RPC_BYTES)
                data=json.dumps(response,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
                if len(data)>MAX_RPC_BYTES: raise ValueError('response limit')
            except Exception:
                data=json.dumps(_error(response.get('id'),-32603,'response exceeds limit')).encode()
            output_stream.write(data+b'\n')
            output_stream.flush()
        if oversized: return
