"""JSON-LD 1.1 子集引擎的单元测试：上下文作用域、展开与拒绝情形。"""
import unittest

from workbench.jsonld11 import (
    ActiveContext,
    JsonLdSubsetError,
    RemoteContextError,
    XSD_STRING,
    process_context,
    resolve_iri,
    resolve_property,
    resolve_reference,
)

DOC_BASE = 'https://school.invalid/show/page.html'


def fresh_ctx():
    return ActiveContext(document_base=DOC_BASE)


class ContextTests(unittest.TestCase):
    def test_vocab_base_language(self):
        ctx = process_context(
            {'@base': '/root/', '@vocab': 'https://schema.org/',
             '@language': 'zh'}, fresh_ctx())
        self.assertEqual(ctx.base, 'https://school.invalid/root/')
        self.assertEqual(ctx.vocab, 'https://schema.org/')
        self.assertEqual(ctx.language, 'zh')
        self.assertEqual(resolve_property('name', ctx),
                         'https://schema.org/name')
        self.assertEqual(resolve_reference('robot-x', ctx),
                         'https://school.invalid/root/robot-x')

    def test_prefix_expansion(self):
        ctx = process_context(
            {'ex': 'https://example.invalid/ns#',
             'term': 'ex:Thing'}, fresh_ctx())
        self.assertTrue(ctx.terms['ex'].prefix)
        self.assertFalse(ctx.terms['term'].prefix)
        self.assertEqual(resolve_property('ex:name', ctx),
                         'https://example.invalid/ns#name')
        self.assertEqual(ctx.terms['term'].iri,
                         'https://example.invalid/ns#Thing')

    def test_prefix_defined_with_trailing_colon(self):
        ctx = process_context(
            {'ex:': 'https://example.invalid/ns/'}, fresh_ctx())
        self.assertTrue(ctx.terms['ex:'].prefix)
        self.assertEqual(resolve_property('ex:name', ctx),
                         'https://example.invalid/ns/name')

    def test_non_prefix_term_cannot_be_used_as_prefix(self):
        ctx = process_context(
            {'term': 'https://example.invalid/Thing'}, fresh_ctx())
        self.assertFalse(ctx.terms['term'].prefix)
        with self.assertRaises(JsonLdSubsetError):
            resolve_property('term:name', ctx)

    def test_term_renaming_object_forms(self):
        ctx = process_context({
            '@vocab': 'https://schema.org/',
            'maker': {'@id': 'creator', '@type': '@id'},
            'zh': {'@id': 'name', '@language': 'zh'},
        }, fresh_ctx())
        self.assertEqual(ctx.terms['maker'].iri,
                         'https://schema.org/creator')
        self.assertTrue(ctx.terms['maker'].type_id)
        self.assertEqual(ctx.terms['zh'].language, 'zh')

    def test_context_array_overrides_and_null_reset(self):
        ctx = process_context([
            {'@vocab': 'https://one.invalid/', '@language': 'en'},
            {'@language': 'zh'},
            None,
        ], fresh_ctx())
        # null 重置：词汇与语言都回到文档初始状态。
        self.assertIsNone(ctx.vocab)
        self.assertIsNone(ctx.language)
        self.assertEqual(ctx.base, DOC_BASE)
        with self.assertRaises(JsonLdSubsetError):
            resolve_property('anything', ctx)

    def test_nested_context_does_not_leak_to_sibling(self):
        outer = process_context(
            {'@vocab': 'https://schema.org/'}, fresh_ctx())
        child = process_context(
            {'name': 'https://local.invalid/title'}, outer)
        # 子作用域看到重命名，外层兄弟节点仍只认识旧词汇。
        self.assertEqual(resolve_property('name', child),
                         'https://local.invalid/title')
        self.assertEqual(resolve_property('name', outer),
                         'https://schema.org/name')
        # 外层上下文对象本身没有被修改。
        self.assertNotIn('name', outer.terms)

    def test_base_scoping(self):
        outer = process_context({'@base': '/root/'}, fresh_ctx())
        inner = process_context({'@base': 'sub/'}, outer)
        self.assertEqual(inner.base, 'https://school.invalid/root/sub/')
        self.assertEqual(outer.base, 'https://school.invalid/root/')
        back = process_context({'@base': None}, inner)
        self.assertIsNone(back.base)

    def test_circular_term_definitions_detected(self):
        with self.assertRaises(JsonLdSubsetError):
            process_context({'a': 'b', 'b': 'a'}, fresh_ctx())
        with self.assertRaises(JsonLdSubsetError):
            process_context({'x': 'x'}, fresh_ctx())
        # 前向引用但无环时正常。
        ctx = process_context(
            {'later': 'earlier', 'earlier': 'https://x.invalid/#t'},
            fresh_ctx())
        self.assertEqual(ctx.terms['later'].iri,
                         'https://x.invalid/#t')

    def test_remote_context_reported_not_loaded(self):
        with self.assertRaises(RemoteContextError):
            process_context('https://remote.invalid/context', fresh_ctx())
        with self.assertRaises(RemoteContextError):
            process_context([{'@vocab': 'https://v.invalid/'},
                             'https://remote.invalid/c'], fresh_ctx())

    def test_unsupported_keywords_rejected(self):
        for bad in (
            {'@container': '@set'},
            {'term': {'@id': 'x', '@reverse': 'y'}},
            {'@direction': 'ltr'},
            {'term': {'@id': 'x', '@type': '@vocab'}},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(JsonLdSubsetError):
                    process_context(bad, fresh_ctx())

    def test_blank_label_and_absolute_iri_passthrough(self):
        ctx = process_context(
            {'@vocab': 'https://schema.org/'}, fresh_ctx())
        self.assertEqual(resolve_reference('_:team', ctx), '_:team')
        self.assertEqual(resolve_reference('https://x.invalid/p', ctx),
                         'https://x.invalid/p')

    def test_resolve_iri_uses_vocab(self):
        ctx = process_context(
            {'@vocab': 'https://schema.org/'}, fresh_ctx())
        self.assertEqual(resolve_iri('creator', ctx),
                         'https://schema.org/creator')


if __name__ == '__main__':
    unittest.main()
