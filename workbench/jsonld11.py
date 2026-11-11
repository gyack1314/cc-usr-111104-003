"""JSON-LD 1.1 子集的本地上下文处理与 IRI 展开。

支持范围以 docs/source-format.md 为界：
- 本地上下文：对象、数组（按顺序覆盖）、null 重置为文档初始上下文；
- @base / @vocab / @language；
- 术语定义：字符串，或带 @id、@type=@id、@language 的对象，支持前缀 IRI；
- 数据关键字：@context、@id、@type、@value、@language、@list、@graph。

明确不支持：远程 @context（只报告不联网）、@reverse、@included、
@container、@direction 等其余关键字。遇到无法按子集解释的输入抛出
JsonLdSubsetError，由调用方隔离对应 script，不猜测含义。
"""
from dataclasses import dataclass
from urllib.parse import urljoin

RDF_NS = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'
XSD_NS = 'http://www.w3.org/2001/XMLSchema#'
RDF_TYPE = RDF_NS + 'type'
RDF_FIRST = RDF_NS + 'first'
RDF_REST = RDF_NS + 'rest'
RDF_NIL = RDF_NS + 'nil'
RDF_LANG_STRING = RDF_NS + 'langString'
XSD_STRING = XSD_NS + 'string'
XSD_BOOLEAN = XSD_NS + 'boolean'
XSD_INTEGER = XSD_NS + 'integer'
XSD_DOUBLE = XSD_NS + 'double'

DATA_KEYWORDS = frozenset({
    '@context', '@id', '@type', '@value', '@language', '@list', '@graph',
})
CONTEXT_KEYWORDS = frozenset({'@base', '@vocab', '@language'})
TERM_OBJECT_KEYS = frozenset({'@id', '@type', '@language'})


class JsonLdSubsetError(Exception):
    """脚本无法按约定子集解释，调用方应隔离整个 script。"""


class RemoteContextError(JsonLdSubsetError):
    """上下文引用远程文档；按约定不自动出网加载。"""


def _has_scheme(value):
    """RFC 3986 scheme = 首字母后接字母数字/./+/- 再冒号。"""
    if not isinstance(value, str) or ':' not in value:
        return False
    head = value.split(':', 1)[0]
    if not head or not head[0].isalpha():
        return False
    return all(c.isalnum() or c in '+-.' for c in head[1:])


@dataclass(frozen=True)
class Term:
    """一个术语或前缀的展开定义。"""
    iri: str
    prefix: bool = False
    type_id: bool = False
    language: str | None = None


class ActiveContext:
    """某一 JSON 位置上的活动上下文；派生时复制，兄弟节点互不影响。"""
    __slots__ = ('base', 'vocab', 'language', 'terms', 'document_base')

    def __init__(self, document_base, base=None, vocab=None,
                 language=None, terms=None):
        self.document_base = document_base
        self.base = document_base if base is None else base
        self.vocab = vocab
        self.language = language
        self.terms = dict(terms) if terms else {}

    def child(self):
        return ActiveContext(self.document_base, self.base, self.vocab,
                             self.language, self.terms)

    def reset(self):
        return ActiveContext(self.document_base)


def _resolve_against_base(ctx, value):
    """相对 IRI 按当前上下文基址解析；绝对 IRI 原样返回。"""
    if value.startswith('_:') or _has_scheme(value):
        return value
    if ctx.base is None:
        raise JsonLdSubsetError(f'相对 IRI {value!r} 没有可用的基址')
    return urljoin(ctx.base, value)


def expand_iri(value, ctx, *, vocab, creating=None):
    """按 JSON-LD 1.1 IRI 展开规则处理一个字符串。

    vocab=True 用于属性与类型（走 @vocab）；vocab=False 用于文档相对引用。
    creating 为正在定义的术语名集合，用于发现定义循环。
    无法展开且不允许文档相对处理时抛 JsonLdSubsetError。
    """
    if not isinstance(value, str) or not value:
        raise JsonLdSubsetError(f'待展开的 IRI 非法：{value!r}')
    if value.startswith('@'):
        return value
    if value.startswith('_:'):
        return value

    creating = creating or frozenset()

    # 1. 精确术语匹配。
    term = ctx.terms.get(value)
    if term is not None:
        if value in creating:
            raise JsonLdSubsetError(
                f'术语定义存在循环展开：{" -> ".join(sorted(creating))} -> {value}')
        return term.iri

    # 2. 已定义前缀的紧凑 IRI（优先于绝对 IRI 判定，JSON-LD 1.1 规则）。
    if ':' in value:
        prefix, suffix = value.split(':', 1)
        prefix_term = ctx.terms.get(prefix)
        if prefix_term is not None:
            if prefix in creating:
                raise JsonLdSubsetError(
                    f'前缀定义存在循环展开：{" -> ".join(sorted(creating))} -> {prefix}')
            if not prefix_term.prefix:
                raise JsonLdSubsetError(
                    f'术语 {prefix!r} 不是前缀，不能用于 {value!r}')
            return prefix_term.iri + suffix

    # 3. 绝对 IRI 原样返回。
    if _has_scheme(value):
        return value

    # 4. 未定义前缀的紧凑 IRI。
    if ':' in value:
        if vocab and ctx.vocab is not None:
            return ctx.vocab + value
        if not vocab:
            return value
        raise JsonLdSubsetError(
            f'紧凑 IRI {value!r} 的前缀 {value.split(":", 1)[0]!r} 未定义')

    # 5. 裸术语：属性位置走 @vocab，文档相对位置留给基址解析。
    if vocab:
        if ctx.vocab is not None:
            return ctx.vocab + value
        raise JsonLdSubsetError(f'术语 {value!r} 未定义且没有 @vocab 可展开')
    return value


def resolve_reference(value, ctx):
    """节点引用的展开：空白标签原样返回，其余按文档相对地址解析。"""
    expanded = expand_iri(value, ctx, vocab=False)
    if expanded.startswith('_:'):
        return expanded
    return _resolve_against_base(ctx, expanded)


def resolve_iri(value, ctx, creating=None):
    """术语定义中 @id 的展开：先做完整 IRI 展开（含 @vocab），
    再把剩余相对 IRI 解析到当前基址。空白标签原样返回。"""
    expanded = expand_iri(value, ctx, vocab=True, creating=creating)
    if expanded.startswith('_:'):
        return expanded
    if _has_scheme(expanded):
        return expanded
    return _resolve_against_base(ctx, expanded)


def resolve_property(key, ctx):
    """属性名必须展开为绝对 IRI；关键字原样返回。"""
    if key.startswith('@'):
        return key
    return expand_iri(key, ctx, vocab=True)


def process_context(value, ctx, path='$'):
    """处理任意位置的 @context，返回新的活动上下文。"""
    if value is None:
        return ctx.reset()
    if isinstance(value, str):
        raise RemoteContextError(
            f'{path}: 远程上下文 {value!r} 未解析（子集不自动出网加载）')
    if isinstance(value, list):
        current = ctx
        for index, item in enumerate(value):
            current = process_context(item, current, f'{path}[{index}]')
        return current
    if not isinstance(value, dict):
        raise JsonLdSubsetError(f'{path}: @context 必须是对象、数组或 null')

    new_ctx = ctx.child()

    for key in CONTEXT_KEYWORDS:
        if key not in value:
            continue
        setting = value[key]
        if key == '@base':
            if setting is None:
                new_ctx.base = None
            elif isinstance(setting, str):
                new_ctx.base = _resolve_against_base(new_ctx, setting)
            else:
                raise JsonLdSubsetError(f'{path}.@base 必须是字符串或 null')
        elif key == '@vocab':
            if setting is None:
                new_ctx.vocab = None
            elif isinstance(setting, str):
                if setting.startswith('_:'):
                    raise JsonLdSubsetError(f'{path}.@vocab 不能是空白节点标签')
                new_ctx.vocab = _resolve_against_base(new_ctx, setting)
            else:
                raise JsonLdSubsetError(f'{path}.@vocab 必须是字符串或 null')
        else:
            if setting is not None and not isinstance(setting, str):
                raise JsonLdSubsetError(f'{path}.@language 必须是字符串或 null')
            new_ctx.language = setting

    definitions = {
        key: raw for key, raw in value.items()
        if not key.startswith('@')
    }
    for key in value:
        if key.startswith('@') and key not in CONTEXT_KEYWORDS:
            raise JsonLdSubsetError(f'{path}: 上下文不支持关键字 {key!r}')

    # null 定义先移除术语；其余定义两阶段处理，使前向引用也能检出循环。
    for name, raw in definitions.items():
        if raw is None:
            new_ctx.terms.pop(name, None)
            if name.endswith(':'):
                new_ctx.terms.pop(name[:-1], None)
    pending = {n: r for n, r in definitions.items() if r is not None}
    resolved: set[str] = set()
    creating: set[str] = set()

    def expand_in_def(raw_value):
        """术语定义值的展开：本批待定义术语可被前向引用。

        按 JSON-LD 1.1，术语定义的 @id 以 IRI 形式展开（走 @vocab 与
        前缀），仍是相对 IRI 的再解析到当前基址；空白标签原样返回。
        """
        if raw_value in pending and raw_value not in resolved:
            return define(raw_value)
        return resolve_iri(raw_value, new_ctx, creating=frozenset(creating))

    def define(name):
        if name in resolved:
            return new_ctx.terms[name].iri
        if name in creating:
            chain = ' -> '.join(sorted(creating) + [name])
            raise JsonLdSubsetError(f'{path}: 术语定义存在循环展开（{chain}）')
        raw = pending[name]
        creating.add(name)
        try:
            if isinstance(raw, str):
                iri = expand_in_def(raw)
                # 前缀术语：定义以冒号结尾，或词汇 IRI 以 #// 结尾。
                term = Term(
                    iri,
                    prefix=raw.endswith(':') or iri.endswith(('#', '/')))
            elif isinstance(raw, dict):
                unknown = set(raw) - TERM_OBJECT_KEYS
                if unknown:
                    raise JsonLdSubsetError(
                        f'{path}.{name}: 术语定义不支持字段 {sorted(unknown)!r}')
                if '@id' not in raw:
                    raise JsonLdSubsetError(
                        f'{path}.{name}: 术语定义缺少 @id')
                iri = expand_in_def(raw['@id'])
                type_setting = raw.get('@type')
                if type_setting is not None and type_setting != '@id':
                    raise JsonLdSubsetError(
                        f'{path}.{name}: 子集仅支持 @type 为 @id，收到 {type_setting!r}')
                language = raw.get('@language')
                if language is not None and not isinstance(language, str):
                    raise JsonLdSubsetError(
                        f'{path}.{name}: @language 必须是字符串')
                term = Term(iri, prefix=False,
                            type_id=type_setting == '@id', language=language)
            else:
                raise JsonLdSubsetError(
                    f'{path}.{name}: 术语定义必须是字符串、对象或 null')
        finally:
            creating.discard(name)
        new_ctx.terms[name] = term
        # 冒号结尾的前缀定义（如 "ex:"）也要能按不带冒号的前缀名查到。
        if term.prefix and name.endswith(':') and name[:-1]:
            new_ctx.terms.setdefault(name[:-1], term)
        resolved.add(name)
        return iri

    for name in sorted(pending):
        define(name)
    return new_ctx
