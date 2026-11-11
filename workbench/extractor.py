"""从网页中抽取全部 application/ld+json 块并生成可查证的关系数据。

输出是确定性的 JSON 字典：节点、带类型的边、有序列表、script 隔离记录、
页面完整性摘要以及原始字节指纹。同一输入重复运行得到逐字节一致的结果。

设计约束见 docs/source-format.md 与 docs/extraction-format.md：
- 读取页面内全部 ld+json script，而不是只取第一个；
- 空白节点标签作用域以页面为界，同页 script 共享、跨页不共享；
- 匿名节点按出现位置编号，字段相同也不合并；
- 全局 IRI 节点可汇集陈述，每条陈述保留页面/script/JSON 路径/抓取版本；
- 普通数组是无序多值（重复原位置保留），@list 保留顺序与重复元素；
- 远程上下文、非法 JSON 等问题只隔离对应 script 并解释原因。
"""
import json
from html.parser import HTMLParser
from pathlib import Path

from .jsonld11 import (
    JsonLdSubsetError,
    RemoteContextError,
    ActiveContext,
    RDF_FIRST,
    RDF_LANG_STRING,
    RDF_NIL,
    RDF_REST,
    RDF_TYPE,
    XSD_BOOLEAN,
    XSD_DOUBLE,
    XSD_INTEGER,
    XSD_STRING,
    process_context,
    resolve_property,
    resolve_reference,
)
from .raw_store import save_bytes, read_bytes

LD_JSON_TYPE = 'application/ld+json'


class ScriptExtractionError(Exception):
    """HTML 层面无法继续抽取（例如字节解码失败）。"""


class _LdScriptParser(HTMLParser):
    """收集全部 type=application/ld+json 的 script 原文与页面标题。

    HTML 的 <base> 标签只记录、不参与 JSON-LD 的基址解析：
    文档缺省基址始终取最终响应 URL。
    """
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self.title = None
        self.base_href = None
        self._in_ld = False
        self._buf: list[str] = []
        self._in_title = False
        self._title_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script':
            content_type = (attrs.get('type') or '').split(';', 1)[0]
            content_type = content_type.strip().lower()
            if content_type == LD_JSON_TYPE:
                self._in_ld = True
                self._buf = []
        elif tag == 'title':
            self._in_title = True
            self._title_buf = []
        elif tag == 'base' and self.base_href is None:
            self.base_href = attrs.get('href')

    def handle_endtag(self, tag):
        if tag == 'script' and self._in_ld:
            self.blocks.append({'raw_text': ''.join(self._buf)})
            self._in_ld = False
            self._buf = []
        elif tag == 'title' and self._in_title:
            self.title = ''.join(self._title_buf).strip() or None
            self._in_title = False

    def handle_data(self, data):
        if self._in_ld:
            self._buf.append(data)
        elif self._in_title:
            self._title_buf.append(data)


def parse_html_blocks(html_bytes, *, encoding='utf-8'):
    """返回 (blocks, title, base_href)；块内原文保留脚本里的精确文本。"""
    try:
        text = html_bytes.decode(encoding)
    except UnicodeDecodeError as exc:
        raise ScriptExtractionError(f'页面无法按 {encoding} 解码：{exc}') from exc
    parser = _LdScriptParser()
    parser.feed(text)
    parser.close()
    return parser.blocks, parser.title, parser.base_href


def literal_for(value, language):
    """把 JSON 标量变成带类型区分的值对象。"""
    if isinstance(value, bool):
        datatype, lexical = XSD_BOOLEAN, 'true' if value else 'false'
    elif isinstance(value, int):
        datatype, lexical = XSD_INTEGER, str(value)
    elif isinstance(value, float):
        datatype, lexical = XSD_DOUBLE, _format_float(value)
    elif isinstance(value, str):
        datatype, lexical = XSD_STRING, value
    else:  # 理论上不可达：None 与容器由调用方处理
        raise JsonLdSubsetError(f'不支持的字面量值：{value!r}')
    return {'kind': 'literal', 'value': lexical, 'datatype': datatype,
            'language': language if isinstance(value, str) and language else None}


def _format_float(value):
    text = repr(value)
    return text


def lang_literal(value, language):
    return {'kind': 'lang_string', 'value': value,
            'datatype': RDF_LANG_STRING, 'language': language}


def ref_literal(iri):
    return {'kind': 'iri', 'value': iri, 'datatype': None, 'language': None}


def node_ref(identifier):
    return {'kind': 'node_ref', 'id': identifier}


class _PageProcessor:
    """单个页面（可能含多个 script）的遍历状态机。"""

    def __init__(self, page_url, page_key, crawl_version):
        self.page_url = page_url
        self.page_key = page_key
        self.crawl_version = crawl_version
        self.blank_labels: dict[str, str] = {}
        self.anonymous_counter = 0
        self.list_counter = 0
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self.lists: list[dict] = []
        self.null_properties: list[dict] = []

    # -- 节点身份 ------------------------------------------------------
    def _label_id(self, label):
        """同页 script 共享显式空白标签作用域；跨页 page_key 不同。"""
        if label not in self.blank_labels:
            self.blank_labels[label] = f'{self.page_key}#b:{label[2:]}'
        return self.blank_labels[label]

    def _anonymous_id(self):
        self.anonymous_counter += 1
        return f'{self.page_key}#b:anon-{self.anonymous_counter}'

    def _list_id(self):
        self.list_counter += 1
        return f'{self.page_key}#b:list-{self.list_counter}'

    def _ensure_node(self, identifier, kinds, provenance):
        record = self.nodes.get(identifier)
        if record is None:
            record = {
                'id': identifier,
                'kinds': sorted(set(kinds)),
                'first_seen': dict(provenance),
            }
            self.nodes[identifier] = record
        else:
            merged = sorted(set(record['kinds']) | set(kinds))
            record['kinds'] = merged
        return record

    # -- 溯源 ----------------------------------------------------------
    def _provenance(self, script_index, path):
        return {'page': self.page_url, 'crawl_version': self.crawl_version,
                'script_index': script_index, 'json_path': path}

    # -- 入口 ----------------------------------------------------------
    def process_script(self, document, script_index):
        ctx = ActiveContext(document_base=self.page_url)
        path = '$'
        if isinstance(document, dict):
            self._process_node(document, ctx, script_index, path, None)
        elif isinstance(document, list):
            for i, item in enumerate(document):
                if not isinstance(item, dict):
                    raise JsonLdSubsetError(
                        f'{path}[{i}]: 顶层数组元素必须是节点对象')
                self._process_node(item, ctx, script_index, f'{path}[{i}]', None)
        else:
            raise JsonLdSubsetError('JSON-LD 文档顶层必须是对象或数组')

    # -- 节点对象 ------------------------------------------------------
    def _process_node(self, obj, ctx, script_index, path, id_hint):
        if not isinstance(obj, dict):
            raise JsonLdSubsetError(f'{path}: 期望节点对象')
        if '@value' in obj:
            raise JsonLdSubsetError(f'{path}: 值对象不能出现在节点位置')
        if '@list' in obj:
            raise JsonLdSubsetError(f'{path}: @list 只能作为属性值出现')
        unknown_keywords = [k for k in obj if k.startswith('@')
                            and k not in ('@context', '@id', '@type', '@graph')]
        if unknown_keywords:
            raise JsonLdSubsetError(
                f'{path}: 不支持的关键字 {sorted(unknown_keywords)!r}')

        local_ctx = ctx
        if '@context' in obj:
            local_ctx = process_context(
                obj['@context'], ctx, f'{path}.@context')

        kinds = ['node']
        identifier = id_hint
        if '@id' in obj:
            raw_id = obj['@id']
            if not isinstance(raw_id, str):
                raise JsonLdSubsetError(f'{path}.@id 必须是字符串')
            identifier = resolve_reference(raw_id, local_ctx)
            kinds.append('blank' if identifier.startswith('_:') or '#b:' in identifier
                         else 'iri')
        if identifier is None:
            identifier = self._anonymous_id()
            kinds.extend(('blank', 'anonymous'))
        elif identifier.startswith('_:'):
            identifier = self._label_id(identifier)
            kinds.extend(('blank', 'labeled'))
        elif '#b:' in identifier:
            kinds.append('blank')
        else:
            kinds.append('iri')

        provenance = self._provenance(script_index, path)
        self._ensure_node(identifier, kinds, provenance)

        if '@type' in obj:
            self._process_type(obj['@type'], local_ctx, identifier,
                              script_index, f'{path}.@type')

        for key in obj:
            if key in ('@context', '@id', '@type', '@graph'):
                continue
            predicate = resolve_property(key, local_ctx)
            term = local_ctx.terms.get(key)
            self._process_property(
                obj[key], local_ctx, identifier, predicate, term,
                script_index, f'{path}.{key}')

        if '@graph' in obj:
            graph = obj['@graph']
            if not isinstance(graph, list):
                raise JsonLdSubsetError(f'{path}.@graph 必须是数组')
            for i, item in enumerate(graph):
                if not isinstance(item, dict):
                    raise JsonLdSubsetError(
                        f'{path}.@graph[{i}]: 图元素必须是节点对象')
                self._process_node(
                    item, local_ctx, script_index, f'{path}.@graph[{i}]', None)
        return identifier

    def _process_type(self, raw_types, ctx, subject, script_index, path):
        types = raw_types if isinstance(raw_types, list) else [raw_types]
        for i, raw in enumerate(types):
            if not isinstance(raw, str):
                raise JsonLdSubsetError(f'{path}[{i}]: @type 必须是字符串')
            type_iri = resolve_property(raw, ctx)
            if type_iri.startswith('@'):
                raise JsonLdSubsetError(f'{path}[{i}]: 非法类型 {raw!r}')
            self._emit_edge(subject, RDF_TYPE, ref_literal(type_iri),
                            self._provenance(script_index,
                                             path if len(types) == 1 else f'{path}[{i}]'))

    # -- 属性值 --------------------------------------------------------
    def _process_property(self, value, ctx, subject, predicate, term,
                          script_index, path):
        prov = self._provenance(script_index, path)
        if value is None:
            # 显式 null：记录但不产生陈述，与缺失、空字符串严格区分。
            self.null_properties.append({
                'subject': subject, 'predicate': predicate,
                'provenance': prov,
            })
            return
        if isinstance(value, list):
            # 普通数组：无序多值，重复元素的每个原位置都各自保留。
            for i, item in enumerate(value):
                self._process_single_value(
                    item, ctx, subject, predicate, term,
                    script_index, f'{path}[{i}]')
            return
        self._process_single_value(
            value, ctx, subject, predicate, term, script_index, path)

    def _process_single_value(self, value, ctx, subject, predicate, term,
                              script_index, path):
        prov = self._provenance(script_index, path)
        if value is None:
            self.null_properties.append(
                {'subject': subject, 'predicate': predicate,
                 'provenance': prov})
            return
        if isinstance(value, dict):
            if '@value' in value:
                literal = self._process_value_object(
                    value, ctx, term, script_index, path)
                self._emit_edge(subject, predicate, literal, prov)
            elif '@list' in value:
                list_id = self._process_list(
                    value, ctx, term, script_index, path)
                self._emit_edge(subject, predicate, node_ref(list_id), prov)
            else:
                target = self._process_node(
                    value, ctx, script_index, path, None)
                self._emit_edge(subject, predicate, node_ref(target), prov)
            return
        if isinstance(value, list):
            # 普通数组嵌套在单值入口（@list 元素除外，列表元素在专门方法处理）。
            raise JsonLdSubsetError(f'{path}: 嵌套数组不能作为属性值')
        literal = self._scalar_literal(value, ctx, term, prov)
        self._emit_edge(subject, predicate, literal, prov)

    def _scalar_literal(self, value, ctx, term, provenance=None):
        if term is not None and term.type_id:
            if not isinstance(value, str):
                raise JsonLdSubsetError('@type=@id 的术语值必须是字符串 IRI')
            reference = resolve_reference(value, ctx)
            if reference.startswith('_:'):
                # 空白标签先在页面作用域登记为节点，再以节点引用输出。
                node_id = self._label_id(reference)
                self._ensure_node(
                    node_id, ['blank', 'labeled', 'node'],
                    provenance or self._provenance(-1, '$'))
                return node_ref(node_id)
            return ref_literal(reference)
        if isinstance(value, str):
            language = term.language if term is not None else ctx.language
            if language:
                return lang_literal(value, language)
        return literal_for(value, ctx.language)

    # -- 值对象 --------------------------------------------------------
    def _process_value_object(self, obj, ctx, term, script_index, path):
        allowed = {'@value', '@language', '@type'}
        unknown = set(obj) - allowed
        if unknown:
            raise JsonLdSubsetError(
                f'{path}: 值对象不支持字段 {sorted(unknown)!r}')
        if '@value' not in obj:
            raise JsonLdSubsetError(f'{path}: 值对象缺少 @value')
        value = obj['@value']
        if isinstance(value, (dict, list)):
            raise JsonLdSubsetError(
                f'{path}.@value: 非法值对象（@value 不能是容器）')
        language = obj.get('@language')
        if language is not None and not isinstance(language, str):
            raise JsonLdSubsetError(f'{path}.@language 必须是字符串')
        if '@type' in obj:
            datatype = obj['@type']
            if not isinstance(datatype, str):
                raise JsonLdSubsetError(f'{path}.@type 必须是字符串 IRI')
            datatype = resolve_property(datatype, ctx)
            if datatype.startswith('@'):
                raise JsonLdSubsetError(f'{path}: 非法数据类型 {datatype!r}')
        if language is not None:
            if not isinstance(value, str):
                raise JsonLdSubsetError(
                    f'{path}: 带语言标签的值必须是字符串')
            return lang_literal(value, language)
        if value is None:
            # 值对象里显式给出的 null：仍是一个带数据类型的字面量，
            # 与属性位置的 null（不产生陈述）区分。
            dtype = datatype if '@type' in obj else XSD_STRING
            return {'kind': 'literal', 'value': None, 'datatype': dtype,
                    'language': None}
        if '@type' in obj:
            if isinstance(value, bool):
                lexical = 'true' if value else 'false'
            else:
                lexical = str(value)
            return {'kind': 'literal', 'value': lexical,
                    'datatype': datatype, 'language': None}
        return self._scalar_literal(
            value, ctx, term, self._provenance(script_index, path))

    # -- @list ---------------------------------------------------------
    def _process_list(self, obj, ctx, term, script_index, path):
        if set(obj) - {'@list'}:
            raise JsonLdSubsetError(
                f'{path}: 子集的 @list 对象只允许 @list 字段')
        items = obj['@list']
        if not isinstance(items, list):
            raise JsonLdSubsetError(f'{path}.@list 必须是数组')

        head_id = self._list_id()
        provenance = self._provenance(script_index, path)
        self._ensure_node(head_id, ['blank', 'list', 'anonymous'], provenance)

        # 空 @list 仍产生列表身份（rdf:nil 语义由空 elements 表达）。
        cells = []
        current = head_id
        for i, item in enumerate(items):
            cell_id = current
            self._ensure_node(cell_id, ['blank', 'list_cell', 'anonymous'],
                              provenance)
            value = self._list_element(
                item, ctx, term, script_index, f'{path}.@list[{i}]')
            self._emit_edge(cell_id, RDF_FIRST, value,
                            self._provenance(
                                script_index, f'{path}.@list[{i}]'))
            if i < len(items) - 1:
                next_id = self._list_id()
                self._ensure_node(
                    next_id, ['blank', 'list_cell', 'anonymous'], provenance)
                self._emit_edge(cell_id, RDF_REST, node_ref(next_id),
                                self._provenance(
                                    script_index, f'{path}.@list[{i}]'))
                current = next_id
            else:
                self._emit_edge(cell_id, RDF_REST, ref_literal(RDF_NIL),
                                self._provenance(
                                    script_index, f'{path}.@list[{i}]'))
            cells.append(cell_id)

        self.lists.append({
            'id': head_id,
            'length': len(items),
            'ordered_elements_path': [
                f'{path}.@list[{i}]' for i in range(len(items))
            ],
            'provenance': provenance,
        })
        return head_id

    def _list_element(self, item, ctx, term, script_index, path):
        """列表元素保留顺序与重复；返回边可用的值或节点引用。"""
        if item is None:
            return {'kind': 'literal', 'value': None,
                    'datatype': XSD_STRING, 'language': None}
        if isinstance(item, dict):
            if '@value' in item:
                return self._process_value_object(
                    item, ctx, term, script_index, path)
            if '@list' in item:
                raise JsonLdSubsetError(f'{path}: @list 不允许嵌套 @list')
            target = self._process_node(item, ctx, script_index, path, None)
            return node_ref(target)
        if isinstance(item, list):
            raise JsonLdSubsetError(f'{path}: 列表元素不能是普通数组')
        return self._scalar_literal(
            item, ctx, term, self._provenance(script_index, path))

    # -- 边 ------------------------------------------------------------
    def _emit_edge(self, subject, predicate, obj, provenance):
        self.edges.append({
            'subject': subject,
            'predicate': predicate,
            'object': obj,
            'provenance': provenance,
        })


def extract_page(html_bytes, page_url, *, crawl_version,
                 encoding='utf-8', raw_dir=None, script_texts=None):
    """抽取一个页面，返回该页的完整结果字典。

    page_url 是最终响应地址（相对 IRI 的缺省基址）。script_texts 可显式
    提供已切分的脚本文本；默认从 html_bytes 解析全部 ld+json 块。
    raw_dir 给定时，页面字节与每个脚本原文经 raw_store 按摘要留存。
    """
    page_key = page_url
    processor = _PageProcessor(page_url, page_key, crawl_version)

    if script_texts is None:
        blocks, title, base_href = parse_html_blocks(
            html_bytes, encoding=encoding)
        script_texts = [b['raw_text'] for b in blocks]
    else:
        _, title, base_href = parse_html_blocks(
            html_bytes, encoding=encoding)

    page_sha = save_bytes(raw_dir, html_bytes) if raw_dir else None
    script_records = []
    for index, raw_text in enumerate(script_texts):
        raw_bytes = raw_text.encode('utf-8')
        script_sha = save_bytes(raw_dir, raw_bytes) if raw_dir else None
        record = {
            'script_index': index,
            'raw_sha256': script_sha,
            'status': 'ok',
        }
        try:
            document = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            record.update(status='isolated', error_code='invalid_json',
                          reason=f'JSON 解析失败：{exc.msg}（第 {exc.lineno} 行 '
                                 f'第 {exc.colno} 列）')
            script_records.append(record)
            continue
        try:
            processor.process_script(document, index)
        except RemoteContextError as exc:
            record.update(status='isolated', error_code='remote_context',
                          reason=str(exc))
        except JsonLdSubsetError as exc:
            record.update(status='isolated', error_code='unsupported_subset',
                          reason=str(exc))
        script_records.append(record)

    isolated = [r for r in script_records if r['status'] == 'isolated']
    complete = not isolated
    summary = {
        'page_url': page_url,
        'crawl_version': crawl_version,
        'title': title,
        'html_base_href': base_href,
        'html_base_ignored': base_href is not None,
        'page_sha256': page_sha,
        'script_total': len(script_records),
        'script_ok': sum(1 for r in script_records if r['status'] == 'ok'),
        'script_isolated': len(isolated),
        'node_count': len(processor.nodes),
        'edge_count': len(processor.edges),
        'list_count': len(processor.lists),
        'null_property_count': len(processor.null_properties),
        'complete': complete,
    }
    return {
        'summary': summary,
        'scripts': script_records,
        'nodes': _sorted_nodes(processor.nodes),
        'edges': _sorted_edges(processor.edges),
        'lists': sorted(processor.lists, key=lambda r: r['id']),
        'null_properties': sorted(
            processor.null_properties,
            key=lambda r: (r['subject'], r['predicate'],
                           r['provenance']['script_index'],
                           r['provenance']['json_path'])),
    }


def _sorted_nodes(nodes):
    return [nodes[key] for key in sorted(nodes)]


def _edge_sort_key(edge):
    obj = edge['object']
    obj_key = (obj.get('kind'), obj.get('id') or '',
               obj.get('datatype') or '', obj.get('language') or '',
               obj.get('value') if obj.get('value') is not None else '\x00')
    prov = edge['provenance']
    return (edge['subject'], edge['predicate'], obj_key,
            prov['script_index'], prov['json_path'])


def _sorted_edges(edges):
    return sorted(edges, key=_edge_sort_key)


def extract_pages(page_inputs, *, raw_dir=None, encoding='utf-8'):
    """抽取多个页面并合并为一个稳定可比的资料包。

    page_inputs 每项为字典：html_bytes/html_path、page_url、crawl_version。
    全局 IRI 节点跨页汇集（节点记录合并 kinds），陈述与溯源原样并存；
    空白节点因 page_key 不同天然隔离，不会被合并。
    """
    pages = []
    merged_nodes: dict[str, dict] = {}
    all_edges, all_lists, all_nulls = [], [], []

    for spec in page_inputs:
        page_url = spec['page_url']
        crawl_version = spec['crawl_version']
        if 'html_bytes' in spec:
            html_bytes = spec['html_bytes']
        else:
            html_bytes = Path(spec['html_path']).read_bytes()
        result = extract_page(
            html_bytes, page_url, crawl_version=crawl_version,
            encoding=spec.get('encoding', encoding), raw_dir=raw_dir)
        pages.append(result)
        for node in result['nodes']:
            existing = merged_nodes.get(node['id'])
            if existing is None:
                merged_nodes[node['id']] = {
                    'id': node['id'],
                    'kinds': list(node['kinds']),
                    'first_seen': dict(node['first_seen']),
                }
            else:
                existing['kinds'] = sorted(
                    set(existing['kinds']) | set(node['kinds']))
        all_edges.extend(result['edges'])
        all_lists.extend(result['lists'])
        all_nulls.extend(result['null_properties'])

    bundle = {
        'format': 'jsonld-subset-extract/1',
        'page_count': len(pages),
        'complete': all(p['summary']['complete'] for p in pages),
        'pages': sorted(pages, key=lambda r: (r['summary']['page_url'],
                                              r['summary']['crawl_version'])),
        'nodes': _sorted_nodes(merged_nodes),
        'edges': _sorted_edges(all_edges),
        'lists': sorted(all_lists, key=lambda r: r['id']),
        'null_properties': sorted(
            all_nulls,
            key=lambda r: (r['subject'], r['predicate'],
                           r['provenance']['page'],
                           r['provenance']['script_index'],
                           r['provenance']['json_path'])),
    }
    return bundle


def write_bundle(bundle, out_path):
    """确定性写出：固定键序、无 ASCII 转义、末尾换行，便于稳定比较。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(bundle, ensure_ascii=False, indent=2,
                      sort_keys=False) + '\n'
    out_path.write_text(text, encoding='utf-8')
    return out_path


def read_raw(raw_dir, key):
    """找回原始字节（页面或脚本原文），读取时重新核对摘要。"""
    return read_bytes(raw_dir, key)
