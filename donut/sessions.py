"""Persistent provider sessions for the interactive terminal."""
import asyncio
import json
import os
import shutil
from pathlib import Path


class CodexSession:
    def __init__(self, project, profile, emit, approve):
        self.project, self.profile = project, profile
        self.emit, self.approve = emit, approve
        self.pending = {}
        self.serial = 0
        self.thread = None
        self.turn_done = None

    async def connect(self):
        self.process = await asyncio.create_subprocess_exec(shutil.which('codex') or 'codex', 'app-server', '--stdio', cwd=self.project, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=32 * 1024 * 1024)
        self.reader = asyncio.create_task(self.read())
        await self.request('initialize', {'clientInfo': {'name': 'donut', 'version': '0.2.0'}, 'capabilities': {'experimentalApi': True}})
        await self.send({'method': 'initialized'})

    async def send(self, message):
        self.process.stdin.write((json.dumps(message) + '\n').encode())
        await self.process.stdin.drain()

    async def request(self, method, params=None):
        if self.reader.done(): raise RuntimeError(getattr(self, 'read_error', 'Codex session is closed'))
        self.serial += 1
        identity = self.serial
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        try:
            await self.send({'id': identity, 'method': method, 'params': params or {}})
            return await asyncio.wait_for(future, 90)
        finally:
            self.pending.pop(identity, None)

    async def read(self):
        try:
            async for line in self.process.stdout:
                try: event = json.loads(line)
                except ValueError: continue
                if 'id' in event and 'method' not in event:
                    future = self.pending.get(event['id'])
                    if future and not future.done():
                        if 'error' in event: future.set_exception(RuntimeError(event['error'].get('message', 'Provider error')))
                        else: future.set_result(event.get('result', {}))
                elif 'id' in event:
                    asyncio.create_task(self.handle_request(event))
                else:
                    method, params = event.get('method', ''), event.get('params', {})
                    if method == 'item/agentMessage/delta': self.emit('text', params.get('delta', ''))
                    elif method == 'item/started': self.emit('activity', params.get('item', {}).get('type', 'Working'))
                    elif method == 'thread/tokenUsage/updated': self.usage = params.get('tokenUsage', {})
                    elif method == 'turn/completed' and self.turn_done and not self.turn_done.done():
                        turn = params.get('turn', {})
                        if turn.get('status') in ('failed', 'interrupted'): self.turn_done.set_exception(RuntimeError(str(turn.get('error') or turn['status'])))
                        else: self.turn_done.set_result(None)
        except Exception as error:
            self.read_error = 'Codex stream failed: ' + str(error)
        finally:
            error = RuntimeError(getattr(self, 'read_error', 'Codex session disconnected'))
            for future in list(self.pending.values()) + [self.turn_done]:
                if future and not future.done(): future.set_exception(error)

    async def handle_request(self, event):
        method = event['method']
        if method.endswith('/requestApproval') and ('commandExecution' in method or 'fileChange' in method):
            allowed = await self.approve(method, event.get('params', {}))
            await self.send({'id': event['id'], 'result': {'decision': 'accept' if allowed else 'decline'}})
        else:
            self.emit('error', 'Unsupported provider interaction: ' + method + '; use /native.')
            await self.send({'id': event['id'], 'error': {'code': -32601, 'message': 'This interaction is not supported by Donut yet; use the native client.'}})

    async def query(self, text):
        if not self.thread:
            result = await self.request('thread/start', {'cwd': str(self.project), 'model': self.profile.get('model') or None, 'approvalPolicy': 'on-request', 'sandbox': 'workspace-write'})
            self.thread = result['thread']['id']
        self.turn_done = asyncio.get_running_loop().create_future()
        await self.request('turn/start', {'threadId': self.thread, 'input': [{'type': 'text', 'text': text}]})
        await self.turn_done

    async def interrupt(self):
        if self.thread:
            # The thread view supplies the currently running turn ID.
            result = await self.request('thread/read', {'threadId': self.thread, 'includeTurns': True})
            turns = result.get('thread', {}).get('turns', [])
            if turns: await self.request('turn/interrupt', {'threadId': self.thread, 'turnId': turns[-1]['id']})

    async def models(self):
        models, cursor = [], None
        while True:
            result = await self.request('model/list', {'cursor': cursor})
            models.extend({'id': m['model'], 'name': m.get('displayName', m['model'])} for m in result.get('data', []))
            cursor = result.get('nextCursor')
            if not cursor: return models

    async def account_usage(self):
        return await self.request('account/rateLimits/read')

    async def close(self):
        if self.process.returncode is None:
            self.process.terminate()
            try: await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        await self.reader


class ClaudeSession:
    def __init__(self, project, profile, emit, approve):
        self.project, self.profile = project, profile
        self.emit, self.approve = emit, approve
        self.usage = {}

    async def _connect(self):
        from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, PermissionResultAllow, PermissionResultDeny
        async def permission(name, inputs, context):
            if await self.approve(name, inputs): return PermissionResultAllow(updated_input=inputs)
            return PermissionResultDeny(message='User declined')
        env = {}
        if self.profile.get('claude_config_dir'): env['CLAUDE_CONFIG_DIR'] = str(Path(self.profile['claude_config_dir']).expanduser())
        options = ClaudeAgentOptions(cli_path=shutil.which('claude'), cwd=self.project, env=env, model=self.profile.get('model') or None, setting_sources=['user', 'project', 'local'], can_use_tool=permission, include_partial_messages=True)
        self.client = ClaudeSDKClient(options=options)
        await self.client.connect()
        self.info = await self.client.get_server_info() or {}

    async def _query(self, text):
        from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock, ResultMessage
        await self.client.query(text)
        async for message in self.client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock): self.emit('text', block.text)
                    elif isinstance(block, ToolUseBlock): self.emit('activity', block.name)
            elif isinstance(message, ResultMessage):
                self.usage = {'tokens': message.usage, 'reported_cost_usd': message.total_cost_usd}
                if message.is_error: raise RuntimeError(message.result or 'Claude turn failed')

    async def _models(self):
        return [{'id': m['value'], 'name': m.get('displayName', m['value'])} for m in self.info.get('models', [])]

    async def _account_usage(self):
        return {'session': self.usage, 'account_limits': 'Not exposed by the supported SDK; available in native Claude /usage.'}

    async def interrupt(self): await self.client.interrupt()
    async def _close(self): await self.client.disconnect()

    async def connect(self):
        self.requests = asyncio.Queue()
        self.ready = asyncio.get_running_loop().create_future()
        self.owner = asyncio.create_task(self._serve())
        await self.ready

    async def _serve(self):
        try:
            await self._connect()
        except Exception as error:
            self.ready.set_exception(error)
            return
        self.ready.set_result(None)
        try:
            while True:
                name, args, future = await self.requests.get()
                if name == 'close':
                    future.set_result(None)
                    break
                try:
                    result = await getattr(self, '_' + name)(*args)
                    if not future.done(): future.set_result(result)
                except Exception as error:
                    if not future.done(): future.set_exception(error)
        finally:
            await self._close()

    async def _call(self, name, *args):
        future = asyncio.get_running_loop().create_future()
        await self.requests.put((name, args, future))
        return await future

    async def query(self, text): return await self._call('query', text)
    async def models(self): return await self._call('models')
    async def account_usage(self): return await self._call('account_usage')
    async def close(self):
        if not self.owner.done(): await self._call('close')
        await self.owner


class QwenSession:
    """Keep Qwen's native saved session across turns, including local models."""
    def __init__(self, project, profile, emit, approve):
        self.project, self.profile, self.emit = project, profile, emit
        self.session_id = None
        self.usage = {}
        self.process = None
    async def connect(self): pass
    async def query(self, text):
        from .providers import command, activate
        await asyncio.to_thread(activate, self.profile)
        args, env = command(self.profile, text)
        if self.session_id: args += ['--resume', self.session_id]
        self.process = await asyncio.create_subprocess_exec(*args, cwd=self.project, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=32 * 1024 * 1024)
        async def input_prompt():
            self.process.stdin.write(text.encode())
            await self.process.stdin.drain()
            self.process.stdin.close()
        async def errors(): return await self.process.stderr.read()
        error_reader = asyncio.create_task(errors())
        await input_prompt()
        async for line in self.process.stdout:
            try: event = json.loads(line)
            except ValueError: continue
            if event.get('type') == 'system': self.session_id = event.get('session_id', self.session_id)
            if event.get('type') == 'stream_event':
                item = event.get('event', {})
                block, delta = item.get('content_block', {}), item.get('delta', {})
                if delta.get('type') == 'text_delta': self.emit('text', delta.get('text', ''))
                if block.get('type') == 'tool_use': self.emit('activity', block.get('name', 'Working'))
            if event.get('type') == 'result':
                self.usage = event.get('usage', {})
                if event.get('is_error'): raise RuntimeError('Qwen reported a failed turn')
        code = await self.process.wait()
        stderr = await error_reader
        if code: raise RuntimeError('Qwen failed: ' + stderr.decode(errors='replace')[-500:])
    async def interrupt(self):
        if self.process and self.process.returncode is None: self.process.terminate()
    async def close(self):
        await self.interrupt()
        if self.process: await self.process.wait()
    async def account_usage(self): return {'session': self.usage}
