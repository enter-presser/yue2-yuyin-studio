"""Wire formats only; both protocols share the same bounded music tools."""
import json
from urllib.parse import urlsplit


def endpoint(base, protocol):
    base = base.rstrip('/')
    path = urlsplit(base).path
    if protocol == 'anthropic':
        if path.endswith('/messages'):
            return base
        return base + ('/messages' if path.endswith('/v1') else '/v1/messages')
    if protocol != 'openai':
        raise ValueError('Unsupported protocol')
    if path.endswith('/chat/completions'):
        return base
    return base + ('/v1' if not path else '') + '/chat/completions'


def request_body(model, messages, tools, protocol):
    payload = {'model': model, 'max_tokens': 6000}
    if protocol == 'openai':
        payload['messages'] = [{k: v for k, v in m.items() if not k.startswith('_')} for m in messages]
        if tools:
            payload.update(tools=tools, tool_choice='auto')
        return payload
    if protocol != 'anthropic':
        raise ValueError('Unsupported protocol')
    system, turns = [], []
    for message in messages:
        role, content = message['role'], message.get('content')
        if role == 'system':
            system.append(content)
            continue
        if role == 'tool':
            blocks = [{'type': 'tool_result', 'tool_use_id': message['tool_call_id'], 'content': content}]
            role = 'user'
        elif role == 'assistant' and '_anthropic_content' in message:
            # Signatures and thinking blocks must be returned unchanged, never displayed.
            blocks = message['_anthropic_content']
        else:
            blocks = [{'type': 'text', 'text': content}] if content else []
            for call in message.get('tool_calls') or []:
                blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'],
                               'input': json.loads(call['function']['arguments'])})
        if turns and turns[-1]['role'] == role:
            turns[-1]['content'].extend(blocks)
        else:
            turns.append({'role': role, 'content': list(blocks)})
    payload['messages'] = turns
    if system:
        payload['system'] = '\n\n'.join(system)
    if tools:
        payload['tools'] = [{'name': t['function']['name'], 'description': t['function']['description'],
                             'input_schema': t['function']['parameters']} for t in tools]
        payload['tool_choice'] = {'type': 'auto'}
    return payload


def normalize(data, protocol):
    if protocol == 'openai':
        choice = data['choices'][0]
        if choice.get('finish_reason') == 'length':
            raise ValueError('Truncated response')
        result = choice['message']
        if not isinstance(result, dict):
            raise ValueError('Invalid message')
        result = {**result, 'role': 'assistant'}
    else:
        if data.get('role') != 'assistant' or data.get('stop_reason') in ('max_tokens', 'pause_turn'):
            raise ValueError('Incomplete message')
        blocks = data['content']
        if not isinstance(blocks, list):
            raise ValueError('Invalid blocks')
        texts, calls = [], []
        for block in blocks:
            if not isinstance(block, dict):
                raise ValueError('Invalid block')
            kind = block.get('type')
            if kind == 'text' and isinstance(block.get('text'), str):
                texts.append(block['text'])
            elif kind == 'tool_use' and isinstance(block.get('input'), dict):
                calls.append({'id': block['id'], 'type': 'function', 'function': {
                    'name': block['name'], 'arguments': json.dumps(block['input'], ensure_ascii=False)}})
            elif kind == 'thinking' and isinstance(block.get('thinking'), str) and isinstance(block.get('signature'), str):
                continue
            elif kind == 'redacted_thinking' and isinstance(block.get('data'), str):
                continue
            else:
                raise ValueError('Unsupported block')
        result = {'role': 'assistant', 'content': '\n'.join(texts), 'tool_calls': calls,
                  '_anthropic_content': blocks}
    calls = result.get('tool_calls')
    if calls is None:
        calls = []
    if not isinstance(calls, list) or any(not isinstance(c, dict) or not isinstance(c.get('id'), str)
            or not c['id'] or not isinstance(c.get('function'), dict)
            or not isinstance(c['function'].get('name'), str)
            or not isinstance(c['function'].get('arguments'), str) for c in calls):
        raise ValueError('Invalid tools')
    if len({c['id'] for c in calls}) != len(calls):
        raise ValueError('Duplicate tool ids')
    if not calls and (not isinstance(result.get('content'), str) or not result['content'].strip()):
        raise ValueError('Empty message')
    return result
