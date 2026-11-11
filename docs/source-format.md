# 网页语义资料

JSON-LD子集按1.1处理IRI和局部上下文；术语定义支持字符串或带@id、@type=@id、@language的对象，可用前缀IRI。上下文数组按顺序覆盖，null重置本地上下文。子集不支持@reverse、@included、@container、@direction及远程上下文。属性值为null时不产生陈述；空@list仍产生列表身份。普通数组不隐含顺序，相同陈述的多次原位置分别保留。

HTML的base标签不是JSON-LD的显式@base，缺省文档基址取最终响应URL。exhibit-a同页两个script引用同一空白标签，exhibit-b的同名标签属于另一作用域。JSON-LD中@id空白标签不是可访问URL。子集内容以本说明为界，来源网页没有任何自动抓取逻辑。
