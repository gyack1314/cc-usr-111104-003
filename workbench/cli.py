"""工作台命令：查看本地资料指纹，或抽取网页中的 JSON-LD 关系数据。"""
import argparse
import hashlib
import json
from pathlib import Path

from .extractor import extract_pages, write_bundle


def cmd_inspect(args):
    root = Path(args.directory)
    if not root.is_dir():
        args.error('资料目录不存在')
    rows = []
    for path in sorted(root.rglob('*')):
        if path.is_file():
            data = path.read_bytes()
            rows.append({'path': path.relative_to(root).as_posix(),
                         'size': len(data),
                         'sha256': hashlib.sha256(data).hexdigest()})
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def cmd_extract(args):
    page_inputs = []
    for entry in args.pages:
        if '=' in entry:
            url, path_text = entry.split('=', 1)
        else:
            path_text = entry
            url = args.default_base.rstrip('/') + '/' + Path(path_text).name
        page_inputs.append({
            'html_path': path_text,
            'page_url': url,
            'crawl_version': args.crawl_version,
        })
    bundle = extract_pages(
        page_inputs, raw_dir=args.raw_dir, encoding=args.encoding)
    if args.out:
        target = write_bundle(bundle, args.out)
        print(f'已写出资料包：{target}')
    else:
        print(json.dumps(bundle, ensure_ascii=False, indent=2))
    print(json.dumps({'complete': bundle['complete'],
                      'pages': [p['summary'] for p in bundle['pages']]},
                     ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(prog='workbench')
    sub = parser.add_subparsers(dest='command', required=True)

    inspect_p = sub.add_parser('inspect', help='查看本地资料文件及其字节指纹')
    inspect_p.add_argument('directory')
    inspect_p.set_defaults(func=cmd_inspect)

    extract_p = sub.add_parser(
        'extract', help='抽取网页中全部 application/ld+json 块')
    extract_p.add_argument(
        'pages', nargs='+',
        help='HTML 文件，可加 最终响应URL=路径 形式指定页面地址')
    extract_p.add_argument(
        '--default-base', default='https://example.invalid/show/',
        help='未显式给出 URL 时拼接相对地址用的缺省文档基址')
    extract_p.add_argument('--crawl-version', default='local-1',
                           help='本次抓取版本标识，写入每条陈述的溯源')
    extract_p.add_argument('--encoding', default='utf-8')
    extract_p.add_argument('--raw-dir', help='原始字节留存目录（按摘要存取）')
    extract_p.add_argument('--out', help='资料包输出路径；缺省打印到标准输出')
    extract_p.set_defaults(func=cmd_extract)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
