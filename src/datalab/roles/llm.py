"""OpenAI 兼容接口的三角色适配器；每次调用独立上下文、单次请求不重试。"""
from __future__ import annotations

import json
import re
import time

import httpx

from datalab.contracts import AMOUNT_KINDS, AnalysisPlan
from datalab.execution.analysis_frame import assemble, model_part
from datalab.roles import olist_prompts
from datalab.roles.model_config import ModelProfile
from datalab.roles.prompts import PLANNER_REVIEW, SYSTEM
from datalab.roles.runtime import ModelCallError, ModelReply, Role

PLAN_FIELDS = ('kind', 'group_by', 'start', 'end', 'previous_start', 'previous_end')
# Olist 计划还可以选择时间字段；数据结构与金额来源由服务端按数据版本与已确认口径填写。
OLIST_PLAN_FIELDS = PLAN_FIELDS + ('time_field',)


def handoff_schema(handoff: dict) -> str:
    """由服务端交接判断数据结构：Planner 与 Reviewer 看概况视图，Analyst 看计划。"""
    profile, plan = handoff.get('profile'), handoff.get('plan')
    if (isinstance(profile, dict) and profile.get('schema') == 'olist') or (isinstance(plan, dict) and plan.get('schema') == 'olist'):
        return 'olist'
    return 'orders'
FENCE = re.compile(r'^\s*```[a-zA-Z0-9_-]*\s*\n(.*?)\n?```\s*$', re.DOTALL)


class ChatClient:
    """只负责一次 chat/completions 请求；错误信息不回显密钥或响应原文。"""

    def __init__(self, profile: ModelProfile, *, transport: httpx.BaseTransport | None = None):
        self.profile = profile
        self._transport = transport
        # 逐次用量明细，只含计数与耗时，供成本评测汇总；不记录提示或响应原文。
        self.usage: list[dict] = []

    def complete(self, messages: list[dict], *, timeout: float, max_tokens: int) -> tuple[str, int]:
        # 延迟到真正请求时检查，等待口径确认的任务不需要密钥。
        if not self.profile.configured:
            raise ModelCallError(f'{self.profile.provider} 尚未配置 API Key')
        body = {'model': self.profile.model, 'messages': messages, 'temperature': self.profile.temperature,
                'max_tokens': max_tokens, 'stream': False}
        if self.profile.json_mode:
            body['response_format'] = {'type': 'json_object'}
        headers = {'Authorization': 'Bearer ' + self.profile.api_key, 'Content-Type': 'application/json'}
        started = time.perf_counter()
        try:
            with httpx.Client(transport=self._transport, timeout=timeout, follow_redirects=False) as client:
                response = client.post(self.profile.base_url + '/chat/completions', json=body, headers=headers)
        except httpx.TimeoutException as error:
            raise ModelCallError('模型请求超时') from error
        except httpx.HTTPError as error:
            raise ModelCallError('模型服务连接失败') from error
        if response.status_code != 200:
            raise ModelCallError(f'模型服务返回 HTTP {response.status_code}')
        try:
            payload = response.json()
            choice = payload['choices'][0]
            content = choice['message']['content']
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise ModelCallError('模型响应结构无效') from error
        # 推理模型可能在思考阶段耗尽输出上限，此时正文为空，应报告为截断而非无内容。
        if choice.get('finish_reason') == 'length':
            raise ModelCallError('模型输出达到长度上限被截断')
        if not isinstance(content, str) or not content.strip():
            raise ModelCallError('模型未返回内容')
        usage = payload.get('usage') or {}
        tokens = usage.get('total_tokens')
        if type(tokens) is not int or tokens < 0:
            # 供应商未返回用量时按字符数保守估算，避免预算失效。
            tokens = (sum(len(item['content']) for item in messages) + len(content)) // 2
        details = usage.get('prompt_tokens_details') or {}
        cached = usage.get('prompt_cache_hit_tokens', details.get('cached_tokens', 0))
        self.usage.append({'prompt_tokens': usage.get('prompt_tokens'), 'completion_tokens': usage.get('completion_tokens'),
                           'cached_tokens': cached if type(cached) is int else 0, 'total_tokens': tokens,
                           'seconds': round(time.perf_counter() - started, 3)})
        return content, tokens


def parse_object(text: str) -> dict:
    match = FENCE.match(text)
    text = match.group(1) if match else text
    # strict=False 只放宽字符串内的原始换行/制表符：代码放进 JSON 字符串时模型常不转义它们。
    try:
        value = json.loads(text, strict=False)
    except ValueError:
        start, end = text.find('{'), text.rfind('}')
        try:
            value = json.loads(text[start:end + 1], strict=False) if 0 <= start < end else None
        except ValueError:
            value = None
    if not isinstance(value, dict):
        raise ModelCallError('模型输出不是 JSON 对象')
    return value


class ModelBackend:
    """把模型输出收敛为工作流契约；数据与口径版本由服务端交接固定，不采纳模型给出的值。"""

    def __init__(self, profile: ModelProfile, options: dict | None = None, *, transport: httpx.BaseTransport | None = None):
        self.client = ChatClient(profile, transport=transport)
        self.options = dict(options or {})

    def invoke(self, role, context, *, timeout, token_limit):
        handoff = context['handoff']
        feedback = handoff.get('feedback') if role == Role.ANALYST else None
        if isinstance(feedback, dict) and isinstance(feedback.get('previous_code'), str):
            # 修正反馈只回传模型自己写的函数，框架代码不占上下文。
            handoff = {**handoff, 'feedback': {**feedback, 'previous_code': model_part(feedback['previous_code'])}}
        message = {'instruction': context['instruction'], 'allowed_tools': context['allowed_tools'], 'handoff': handoff}
        if role == Role.PLANNER and self.options:
            message['ui_options'] = {'note': '用户在界面选择的参考选项；与问题冲突时以问题为准', **self.options}
        if handoff_schema(handoff) == 'olist':
            system = olist_prompts.SYSTEM[role.value] + (olist_prompts.REVIEW if role == Role.PLANNER and 'review' in handoff else '')
        else:
            system = SYSTEM[role.value] + (PLANNER_REVIEW if role == Role.PLANNER and 'review' in handoff else '')
        messages = [{'role': 'system', 'content': system},
                    {'role': 'user', 'content': json.dumps(message, ensure_ascii=False)}]
        config = self.client.profile
        reviewer = role == Role.REVIEWER
        request_timeout = config.reviewer_timeout if reviewer else config.request_timeout
        output_limit = config.reviewer_max_output_tokens if reviewer else config.max_output_tokens
        content, tokens = self.client.complete(messages, timeout=max(1.0, min(timeout, request_timeout)),
                                               max_tokens=max(1, min(token_limit, output_limit)))
        reply = parse_object(content)
        usage = dict(self.client.usage[-1]) if self.client.usage else None
        if role == Role.PLANNER:
            return ModelReply(self._plan(reply, handoff), tokens, usage)
        if role == Role.ANALYST:
            code = reply.get('code')
            if set(reply) != {'code'} or not isinstance(code, str) or not code.strip():
                raise ModelCallError('Analyst 输出不符合代码契约')
            match = FENCE.match(code)
            # 模型只写 analyze 函数；计划常量与输出读写由服务端框架提供，组装后仍进入受限容器与独立核验。
            return ModelReply({'code': assemble(handoff['plan'], match.group(1) if match else code)}, tokens, usage)
        if set(reply) != {'accepted', 'feedback'} or type(reply['accepted']) is not bool or not isinstance(reply['feedback'], str):
            raise ModelCallError('Reviewer 输出不符合审查契约')
        return ModelReply(reply, tokens, usage)

    @staticmethod
    def _plan(reply: dict, handoff: dict) -> dict:
        confirmation = reply.get('confirmation')
        if set(reply) == {'confirmation'} and isinstance(confirmation, str) and confirmation.strip():
            return {'confirmation': confirmation.strip()}
        proposal = reply.get('plan')
        if set(reply) != {'plan'} or not isinstance(proposal, dict):
            raise ModelCallError('Planner 输出不符合计划契约')
        metric = handoff['metric']
        olist = handoff_schema(handoff) == 'olist'
        # 空字符串与 null 等价，其余类型交由计划契约校验。
        fields = {key: proposal.get(key) or None for key in (OLIST_PLAN_FIELDS if olist else PLAN_FIELDS)}
        if not isinstance(fields['kind'], str):
            raise ModelCallError('Planner 未给出分析类型')
        if olist:
            if fields['time_field'] is None:
                del fields['time_field']
            # 金额来源是已确认口径的一部分：Planner 写了不同的值即拒绝，不静默改回。
            if proposal.get('amount_source') not in (None, '', metric.get('amount_source')):
                raise ModelCallError('Planner 不能更改已确认的订单金额来源')
            fields['schema'] = 'olist'
            if fields['kind'] in AMOUNT_KINDS:
                fields['amount_source'] = metric.get('amount_source')
        try:
            plan = AnalysisPlan(handoff['dataset_version'], metric['version'],
                                included_statuses=tuple(metric['included_statuses']), **fields)
        except (TypeError, ValueError) as error:
            raise ModelCallError('Planner 计划不满足领域约束') from error
        return {'plan': plan.payload()}
