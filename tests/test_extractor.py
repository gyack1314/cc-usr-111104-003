"""抽取器端到端测试：样本库与资料约定要求的全部区别点。"""
import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

from source_lab.http import start
from workbench.extractor import (
    extract_page,
    extract_pages,
    parse_html_blocks,
    read_raw,
    write_bundle,
)

SOURCES = Path('sources')
A_URL = 'https://school.invalid/show/exhibit-a.html'
B_URL = 'https://school.invalid/show/exhibit-b.html'
U_URL = 'https://school.invalid/show/unsupported.html'
XSD_STRING = 'http://www.w3.org/2001/XMLSchema#string'
RDF_NIL = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#nil'


def load(name):
    return (SOURCES / name).read_bytes()


def edges_for(page, subject=None, predicate=None):
    out = []
    for edge in page['edges']:
        if subject is not None and edge['subject'] != subject:
            continue
        if predicate is not None and edge['predicate'] != predicate:
            continue
        out.append(edge)
    return out


class SamplePageTests(unittest.TestCase):
    def setUp(self):
        self.page_a = extract_page(
            load('exhibit-a.html'), A_URL, crawl_version='crawl-1')
        self.page_b = extract_page(
            load('exhibit-b.html'), B_URL, crawl_version='crawl-1')
        self.page_u = extract_page(
            load('unsupported.html'), U_URL, crawl_version='crawl-1')

    def test_all_ld_json_scripts_are_read(self):
        # 旧脚本只取第一个；这里每个 script 都有记录与独立序号。
        self.assertEqual(self.page_a['summary']['script_total'], 2)
        self.assertEqual(self.page_a['summary']['script_ok'], 2)
        self.assertEqual(self.page_u['summary']['script_total'], 3)
        self.assertEqual(
            [s['script_index'] for s in self.page_u['scripts']], [0, 1, 2])

    def test_html_base_is_ignored_for_jsonld_base(self):
        summary = self.page_a['summary']
        self.assertEqual(summary['html_base_href'], '/ignored/')
        self.assertTrue(summary['html_base_ignored'])
        # 相对 @id robot-a 按 JSON-LD 上下文基址解析，而非 HTML <base>。
        self.assertIn('https://school.invalid/show/robot-a',
                      {n['id'] for n in self.page_a['nodes']})

    def test_shared_blank_label_scope_within_page(self):
        team_a = f'{A_URL}#b:team'
        # 两个 script 中的 _:team 收敛为同一页面作用域节点。
        self.assertIn(team_a, {n['id'] for n in self.page_a['nodes']})
        maker = edges_for(self.page_a,
                          subject='https://school.invalid/show/robot-a',
                          predicate='https://schema.org/creator')
        self.assertEqual(len(maker), 1)
        self.assertEqual(maker[0]['object'],
                         {'kind': 'node_ref', 'id': team_a})
        self.assertEqual(maker[0]['provenance']['script_index'], 0)
        # 第二个 script 给同一节点补充了陈述。
        self.assertTrue(edges_for(self.page_a, subject=team_a,
                                  predicate='https://schema.org/member'))

    def test_blank_labels_never_shared_across_pages(self):
        team_b = f'{B_URL}#b:team'
        self.assertIn(team_b, {n['id'] for n in self.page_b['nodes']})
        bundle = extract_pages([
            {'html_bytes': load('exhibit-a.html'), 'page_url': A_URL,
             'crawl_version': 'crawl-1'},
            {'html_bytes': load('exhibit-b.html'), 'page_url': B_URL,
             'crawl_version': 'crawl-1'},
        ])
        ids = {n['id'] for n in bundle['nodes']}
        self.assertIn(f'{A_URL}#b:team', ids)
        self.assertIn(team_b, ids)
        # 两支队伍是不同节点，绝不因同名标签合并。
        self.assertNotEqual(f'{A_URL}#b:team', team_b)

    def test_anonymous_nodes_are_not_merged_by_equal_fields(self):
        html = (
            '<html><script type="application/ld+json">'
            '{"@context":{"@vocab":"https://schema.org/"},'
            '"@graph":[{"@id":"_:x","name":"同形"},'
            '{"@id":"_:x","name":"同形"}]}'
            '</script></html>'
        ).encode()
        page = extract_page(html, A_URL, crawl_version='v')
        # 同页同标签是同一节点；再用无 @id 的真匿名节点验证不按字段合并。
        html2 = (
            '<html><script type="application/ld+json">'
            '{"@context":{"@vocab":"https://schema.org/"},'
            '"@graph":[{"name":"同形"},{"name":"同形"}]}'
            '</script></html>'
        ).encode()
        page2 = extract_page(html2, A_URL, crawl_version='v')
        # 顶层包装对象也是匿名节点；图内两个同形节点各自独立。
        anon = sorted(n['id'] for n in page2['nodes']
                      if 'anonymous' in n['kinds'])
        self.assertEqual(len(anon), 3)
        self.assertEqual(len(set(anon)), 3)
        # 图内两个节点的来源路径不同。
        graph_paths = {
            n['first_seen']['json_path']
            for n in page2['nodes']
            if n['first_seen']['json_path'].startswith('$.@graph')
        }
        self.assertEqual(
            graph_paths, {'$.@graph[0]', '$.@graph[1]'})
        # 同标签两次出现只登记一个节点。
        labeled = [n for n in page['nodes'] if 'labeled' in n['kinds']]
        self.assertEqual(len(labeled), 1)

    def test_term_rename_and_language_terms(self):
        robot = 'https://school.invalid/show/robot-a'
        names = edges_for(self.page_a, subject=robot,
                          predicate='https://schema.org/name')
        kinds = {(e['object']['kind'], e['object'].get('language'))
                 for e in names}
        self.assertIn(('literal', None), kinds)
        self.assertIn(('lang_string', 'zh'), kinds)
        values = {e['object']['value'] for e in names}
        self.assertEqual(values, {'四足展示', '校园作品'})

    def test_nested_context_scope_does_not_leak(self):
        # member 内嵌 @context 把 name 重命名到 local 词汇，
        # 同一 script 顶层的 name 仍走 schema.org 词汇。
        person_edges = edges_for(
            self.page_a, subject='https://people.invalid/p1')
        self.assertEqual(
            [e['predicate'] for e in person_edges],
            ['https://local.invalid/title'])
        team_edges = edges_for(
            self.page_a, subject=f'{A_URL}#b:team',
            predicate='https://schema.org/name')
        self.assertEqual(len(team_edges), 1)

    def test_list_keeps_order_and_duplicates(self):
        lists = self.page_a['lists']
        self.assertEqual(len(lists), 1)
        record = lists[0]
        self.assertEqual(record['length'], 3)
        self.assertEqual(record['ordered_elements_path'], [
            '$.steps.@list[0]', '$.steps.@list[1]', '$.steps.@list[2]'])
        firsts = [e for e in self.page_a['edges']
                  if e['predicate'] ==
                  'http://www.w3.org/1999/02/22-rdf-syntax-ns#first']
        ordered = [e['object']['value'] for e in
                   sorted(firsts, key=lambda e: e['provenance']['json_path'])]
        # 重复元素“定位”保留两个原位置。
        self.assertEqual(ordered, ['定位', '行走', '定位'])
        rests = [e for e in self.page_a['edges']
                 if e['predicate'] ==
                 'http://www.w3.org/1999/02/22-rdf-syntax-ns#rest']
        self.assertEqual(rests[-1]['object']['kind'], 'iri')
        self.assertEqual(rests[-1]['object']['value'], RDF_NIL)

    def test_empty_list_still_has_identity(self):
        record = self.page_b['lists'][0]
        self.assertEqual(record['length'], 0)
        self.assertEqual(record['ordered_elements_path'], [])
        steps = edges_for(self.page_b,
                          subject='https://school.invalid/show/robot-b',
                          predicate='https://schema.org/steps')
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]['object']['kind'], 'node_ref')
        self.assertEqual(steps[0]['object']['id'], record['id'])

    def test_null_missing_empty_string_are_distinct(self):
        robot = 'https://school.invalid/show/robot-b'
        # 显式 null：只在 null_properties，不产生边。
        nulls = [n for n in self.page_b['null_properties']
                 if n['subject'] == robot]
        self.assertEqual(len(nulls), 1)
        self.assertEqual(nulls[0]['predicate'],
                         'https://schema.org/note')
        self.assertFalse(edges_for(
            self.page_b, subject=robot,
            predicate='https://schema.org/note'))
        # 缺失字段 absent 在任何地方都不出现。
        flat = json.dumps(self.page_b, ensure_ascii=False)
        self.assertNotIn('absent', flat)
        # 空字符串是带语言标签的真实字面量。
        names = edges_for(self.page_b, subject=robot,
                         predicate='https://schema.org/name')
        zh_empty = [e for e in names
                    if e['object'].get('language') == 'zh']
        self.assertEqual(len(zh_empty), 1)
        self.assertEqual(zh_empty[0]['object']['value'], '')
        self.assertEqual(zh_empty[0]['object']['kind'], 'lang_string')

    def test_value_type_distinctions(self):
        html = (
            '<script type="application/ld+json">{'
            '"@context":{"@vocab":"https://schema.org/",'
            '"link":{"@id":"url","@type":"@id"}},'
            '"@id":"https://x.invalid/n",'
            '"plain":"文本","count":3,"flag":true,"ratio":1.5,'
            '"link":"https://y.invalid/",'
            '"tagged":{"@value":"英文","@language":"en"}}'
            '</script>'
        ).encode()
        page = extract_page(html, A_URL, crawl_version='v')
        by_pred = {e['predicate']: e['object'] for e in page['edges']}
        self.assertEqual(by_pred['https://schema.org/plain']['kind'],
                         'literal')
        self.assertEqual(by_pred['https://schema.org/plain']['datatype'],
                         XSD_STRING)
        self.assertEqual(by_pred['https://schema.org/count']['datatype'],
                         'http://www.w3.org/2001/XMLSchema#integer')
        self.assertEqual(by_pred['https://schema.org/count']['value'], '3')
        self.assertEqual(by_pred['https://schema.org/flag']['datatype'],
                         'http://www.w3.org/2001/XMLSchema#boolean')
        self.assertEqual(by_pred['https://schema.org/ratio']['datatype'],
                         'http://www.w3.org/2001/XMLSchema#double')
        self.assertEqual(by_pred['https://schema.org/url']['kind'], 'iri')
        self.assertEqual(by_pred['https://schema.org/url']['value'],
                         'https://y.invalid/')
        self.assertEqual(by_pred['https://schema.org/tagged']['kind'],
                         'lang_string')
        self.assertEqual(by_pred['https://schema.org/tagged']['language'],
                         'en')

    def test_unordered_arrays_keep_duplicate_positions(self):
        html = (
            '<script type="application/ld+json">{'
            '"@context":{"@vocab":"https://schema.org/"},'
            '"@id":"https://x.invalid/n","alias":["a","a","b"]}'
            '</script>'
        ).encode()
        page = extract_page(html, A_URL, crawl_version='v')
        alias = edges_for(page, predicate='https://schema.org/alias')
        self.assertEqual(len(alias), 3)
        paths = [e['provenance']['json_path'] for e in alias]
        self.assertEqual(
            sorted(paths),
            ['$.alias[0]', '$.alias[1]', '$.alias[2]'])
        self.assertEqual(
            sorted(e['object']['value'] for e in alias),
            ['a', 'a', 'b'])

    def test_isolated_scripts_with_reasons(self):
        codes = [s['error_code'] for s in self.page_u['scripts']]
        self.assertEqual(codes,
                         ['remote_context',
                          'unsupported_subset', 'invalid_json'])
        self.assertFalse(self.page_u['summary']['complete'])
        self.assertEqual(self.page_u['summary']['script_ok'], 0)
        # 隔离不影响同页其他块：构造一个好坏混合的页面。
        mixed = (
            '<script type="application/ld+json">{bad}</script>'
            '<script type="application/ld+json">'
            '{"@context":{"@vocab":"https://v.invalid/"},'
            '"@id":"ok","name":"好块"}</script>'
        ).encode()
        page = extract_page(mixed, A_URL, crawl_version='v')
        self.assertEqual(page['summary']['script_total'], 2)
        self.assertEqual(page['summary']['script_ok'], 1)
        self.assertEqual(page['summary']['script_isolated'], 1)
        self.assertFalse(page['summary']['complete'])
        self.assertEqual(page['summary']['node_count'], 1)

    def test_invalid_value_objects_are_rejected(self):
        cases = [
            '{"@value":{"nested":1}}',
            '{"@value":[1,2]}',
            '{"@value":"x","@container":"@set"}',
            '{"@language":123}',
        ]
        for case in cases:
            html = (f'<script type="application/ld+json">'
                    f'{{"@context":{{"@vocab":"https://v.invalid/"}},'
                    f'"@id":"n","bad":{case}}}</script>').encode()
            with self.subTest(case=case):
                page = extract_page(html, A_URL, crawl_version='v')
                self.assertEqual(
                    page['scripts'][0]['error_code'],
                    'unsupported_subset')

    def test_provenance_on_every_statement(self):
        for edge in self.page_a['edges']:
            prov = edge['provenance']
            self.assertEqual(prov['page'], A_URL)
            self.assertEqual(prov['crawl_version'], 'crawl-1')
            self.assertIn(prov['script_index'], (0, 1))
            self.assertTrue(prov['json_path'].startswith('$'))

    def test_replay_is_byte_stable(self):
        first = extract_page(load('exhibit-a.html'), A_URL,
                             crawl_version='crawl-1')
        second = extract_page(load('exhibit-a.html'), A_URL,
                              crawl_version='crawl-1')
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(second, ensure_ascii=False, sort_keys=True))

    def test_multiple_sources_coexist(self):
        # 同一页面两个抓取版本：相同事实以两条带不同版本溯源的边并存。
        v1 = extract_page(load('exhibit-b.html'), B_URL,
                          crawl_version='crawl-1')
        v2 = extract_page(load('exhibit-b.html'), B_URL,
                          crawl_version='crawl-2')
        bundle = extract_pages([
            {'html_bytes': load('exhibit-b.html'), 'page_url': B_URL,
             'crawl_version': 'crawl-1'},
            {'html_bytes': load('exhibit-b.html'), 'page_url': B_URL,
             'crawl_version': 'crawl-2'},
        ])
        # 全局 IRI 节点汇集为一个。
        robot_nodes = [n for n in bundle['nodes']
                       if n['id'] == 'https://school.invalid/show/robot-b']
        self.assertEqual(len(robot_nodes), 1)
        # 每条陈述出现两次，溯源版本不同。
        creator = [e for e in bundle['edges']
                   if e['predicate'] == 'https://schema.org/creator']
        self.assertEqual(len(creator), 2)
        self.assertEqual({e['provenance']['crawl_version']
                          for e in creator}, {'crawl-1', 'crawl-2'})
        self.assertEqual(v1['summary']['node_count'],
                         v2['summary']['node_count'])

    def test_raw_bytes_round_trip_and_summary(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            page = extract_page(
                load('exhibit-a.html'), A_URL,
                crawl_version='crawl-1', raw_dir=raw_dir)
            # 页面字节可按摘要找回并自动核对。
            self.assertEqual(
                read_raw(raw_dir, page['summary']['page_sha256']),
                load('exhibit-a.html'))
            for script in page['scripts']:
                self.assertEqual(
                    read_raw(raw_dir, script['raw_sha256']).decode('utf-8')
                    [:1], '{')
            blocks, _, _ = parse_html_blocks(load('exhibit-a.html'))
            self.assertEqual(len(blocks), 2)

    def test_bundle_write_is_deterministic(self):
        bundle = extract_pages([
            {'html_bytes': load('exhibit-a.html'), 'page_url': A_URL,
             'crawl_version': 'crawl-1'},
            {'html_bytes': load('exhibit-b.html'), 'page_url': B_URL,
             'crawl_version': 'crawl-1'},
            {'html_bytes': load('unsupported.html'), 'page_url': U_URL,
             'crawl_version': 'crawl-1'},
        ])
        with tempfile.TemporaryDirectory() as d:
            p1 = write_bundle(bundle, Path(d) / 'b1.json')
            p2 = write_bundle(bundle, Path(d) / 'b2.json')
            self.assertEqual(p1.read_bytes(), p2.read_bytes())
            reread = json.loads(p1.read_text(encoding='utf-8'))
            self.assertFalse(reread['complete'])
            self.assertEqual(reread['page_count'], 3)

    def test_relative_iri_resolution_against_response_url(self):
        html = (
            '<script type="application/ld+json">{'
            '"@context":{"@vocab":"https://schema.org/"},'
            '"@id":"relative/robot","parent":{"@id":"../parent"}}'
            '</script>'
        ).encode()
        page = extract_page(
            html, 'https://host.invalid/deep/page.html',
            crawl_version='v')
        ids = {n['id'] for n in page['nodes']}
        self.assertIn('https://host.invalid/deep/relative/robot', ids)
        self.assertIn('https://host.invalid/parent', ids)
        parent = [e for e in page['edges']
                  if e['predicate'] == 'https://schema.org/parent']
        self.assertEqual(parent[0]['object'],
                         {'kind': 'node_ref',
                          'id': 'https://host.invalid/parent'})

    def test_graph_context_and_typed_null_literal(self):
        html = (
            '<script type="application/ld+json">{'
            '"@context":{"@vocab":"https://schema.org/"},'
            '"@graph":[{"@id":"https://x.invalid/n",'
            '"typedNull":{"@value":null,"@type":"https://custom/T"}}]}'
            '</script>'
        ).encode()
        page = extract_page(html, A_URL, crawl_version='v')
        edge = edges_for(page, predicate='https://schema.org/typedNull')[0]
        self.assertIsNone(edge['object']['value'])
        self.assertEqual(edge['object']['datatype'],
                         'https://custom/T')
        # 值对象 null 是字面量，不进 null_properties。
        self.assertEqual(page['null_properties'], [])

    def test_top_level_array_and_language_scoping(self):
        html = (
            '<script type="application/ld+json">['
            '{"@context":{"@vocab":"https://v.invalid/","@language":"en"},'
            '"@id":"https://x/1","note":"默认英文"},'
            '{"@context":{"@vocab":"https://v.invalid/"},'
            '"@id":"https://x/2",'
            '"note":{"@value":"中文","@language":"zh"}}]'
            '</script>'
        ).encode()
        page = extract_page(html, 'https://host/page', crawl_version='v')
        notes = {e['subject']: e['object'] for e in page['edges']}
        self.assertEqual(notes['https://x/1']['language'], 'en')
        self.assertEqual(notes['https://x/1']['kind'], 'lang_string')
        self.assertEqual(notes['https://x/2']['language'], 'zh')
        paths = {e['provenance']['json_path'] for e in page['edges']}
        self.assertEqual(paths, {'$[0].note', '$[1].note'})

    def test_multiple_scripts_isolation_does_not_poison_label_scope(self):
        # 第一个块非法被隔离；第二个块的空白标签仍在页面作用域正常登记。
        html = (
            '<script type="application/ld+json">{broken}</script>'
            '<script type="application/ld+json">'
            '{"@context":{"@vocab":"https://v.invalid/"},'
            '"@id":"_:only","name":"幸存块"}</script>'
        ).encode()
        page = extract_page(html, A_URL, crawl_version='v')
        labels = {n['id'] for n in page['nodes']}
        self.assertEqual(labels, {f'{A_URL}#b:only'})
        self.assertEqual(page['scripts'][0]['error_code'], 'invalid_json')
        self.assertEqual(page['scripts'][1]['status'], 'ok')

    def test_script_type_with_profile_parameter(self):
        html = (
            '<script type="application/ld+json; profile=\"https://www.w3.org/ns/json-ld#compacted\">'
            '{"@context":{"@vocab":"https://v.invalid/"},"@id":"n"}'
            '</script>'
        ).encode()
        page = extract_page(html, A_URL, crawl_version='v')
        self.assertEqual(page['summary']['script_total'], 1)
        self.assertTrue(page['summary']['complete'])


class HttpSourceIntegrationTests(unittest.TestCase):
    def test_extract_through_local_source_server(self):
        server = start(SOURCES / 'normal.json')
        try:
            url = f'http://127.0.0.1:{server.server_port}/exhibit-a.html'
            with urllib.request.urlopen(url, timeout=5) as response:
                body = response.read()
                final_url = response.geturl()
            page = extract_page(body, final_url, crawl_version='http-1')
            self.assertTrue(page['summary']['complete'])
            self.assertEqual(page['summary']['script_total'], 2)
            # 相对 IRI 按最终响应地址解析，节点 ID 落在本地服务地址下。
            node_ids = {n['id'] for n in page['nodes']}
            self.assertTrue(
                any(identifier.startswith(f'http://127.0.0.1:{server.server_port}/')
                    for identifier in node_ids))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
