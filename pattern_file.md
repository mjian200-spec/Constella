# 文本格式匹配文件规范

## 1. 目标

工业教材中存在大量具有稳定形式的文本表达，例如：

```text
第5章 CO₂气体保护焊
5.2.3 焊接参数的选择
如图5-18所示
由表5-3可知
在其他条件相同时
随着焊接电流增加，熔深增大
药芯焊丝分为熔渣型和金属粉型
```

这些格式需要支持持续查看、补充、关闭和修正，因此不得散落在 `cleaning.py`、`structure.py`、`assets.py`、`conditions.py` 和 `routing.py` 中。

所有可配置文本格式集中保存在：

```text
configs/context_builder/patterns.yaml
```

格式匹配只负责产生结构化候选或触发已注册处理器，不直接完成资产对齐、条件作用域传播或最终规则判断。

## 2. 配置文件结构

```yaml
version: 1

groups:
  noise: []
  heading: []
  asset_reference: []
  continuation: []
  condition_language: []
  rule_language: []
  ontology_language: []
```

首轮固定使用以上七个规则组。新增规则优先加入已有组，不应随意创建新组。

## 3. 单条规则结构

每条规则统一使用：

```yaml
- id: heading.numbered_section
  description: 匹配5.2或5.2.3形式的小节标题
  enabled: true
  priority: 100

  matcher:
    type: regex
    pattern: '^\s*\d+(?:\.\d+){1,3}\s+.+$'
    flags: []

  conditions:
    unit_types: [title, passage]
    min_length: 2
    max_length: 100

  action:
    type: add_candidate
    target: heading
    value: numbered_section

  confidence: 0.95
  tags: [structure]
```

字段含义：

| 字段 | 含义 |
|---|---|
| `id` | 稳定且唯一的规则编号 |
| `description` | 供开发者查看的中文说明 |
| `enabled` | 是否启用 |
| `priority` | 多条规则冲突时的执行优先级 |
| `matcher` | 匹配方式和匹配内容 |
| `conditions` | 允许应用该规则的Unit范围 |
| `action` | 命中后产生什么候选结果 |
| `confidence` | 规则自身的基础置信度 |
| `tags` | 检索、统计和测试分类 |

规则ID一旦进入正式输出，不应随意修改。修改规则含义时新建ID或提升配置版本。

## 4. 匹配方式

### 4.1 正则匹配

适合编号、显式引用和标点形式：

```yaml
matcher:
  type: regex
  pattern: '图\s*\d+\s*[-—－]\s*\d+'
  flags: []
```

正则必须写在 `patterns.yaml` 中，业务模块不得重新定义相同表达式。

### 4.2 关键词匹配

适合稳定的触发词集合：

```yaml
matcher:
  type: keywords
  mode: any
  values:
    - 如图所示
    - 见下图
    - 由图可知
```

`mode`支持：

```text
any
all
```

关键词命中只能产生候选，不能直接确定具体资产。

### 4.3 模板匹配

适合规则、本体和条件句式：

```yaml
matcher:
  type: template
  pattern: '随着{FACTOR}{CHANGE}，{RESULT}{TREND}'
  slots:
    CHANGE: [增加, 增大, 提高, 减小, 降低]
    TREND: [增加, 增大, 提高, 减小, 降低]
```

模板匹配只表示句子可能包含影响规则，不代表已正确识别变量、方向和条件。

### 4.4 函数处理器

适合不能用单条正则稳定判断的情况：

```yaml
matcher:
  type: handler
  name: detect_cross_page_continuation
```

处理器必须提前注册：

```python
PATTERN_HANDLERS = {
    "detect_cross_page_continuation":
        detect_cross_page_continuation,
}
```

配置文件只能引用已注册处理器，不允许通过配置动态导入任意Python路径。

## 5. 初始规则组

### 5.1 噪声格式 `noise`

用于识别：

- 独立页码；
- 重复页眉；
- 重复页脚；
- OCR产生的空白或无意义字符块；
- 重复扫描块。

示例：

```yaml
- id: noise.page_number_only
  description: 只有阿拉伯数字的独立页码
  enabled: true
  priority: 100
  matcher:
    type: regex
    pattern: '^\s*\d{1,4}\s*$'
    flags: []
  conditions:
    unit_types: [passage]
    require_page_margin: true
  action:
    type: add_role_candidate
    target: noise
  confidence: 0.95
  tags: [cleaning]
```

仅匹配数字不能直接删除内容，还必须结合页边位置。噪声只有达到最终确定条件后才能排除。

### 5.2 标题格式 `heading`

用于识别：

```text
第5章
第十二章
5.2
5.2.3
一、焊接材料
（一）焊接电流
```

示例：

```yaml
- id: heading.chinese_chapter
  description: 中文章节标题
  enabled: true
  priority: 110
  matcher:
    type: regex
    pattern: '^\s*第\s*[0-9一二三四五六七八九十百]+\s*章(?:\s+.+)?$'
    flags: []
  conditions:
    unit_types: [title, passage]
    max_length: 100
  action:
    type: add_candidate
    target: heading
    value: chapter
  confidence: 0.98
  tags: [structure]
```

标题格式只生成标题等级候选，最终层级还要结合目录、版式和相邻编号。

### 5.3 资产引用格式 `asset_reference`

用于识别：

```text
图5-18
表5-3
式（5-21）
如上图所示
由下表可知
图中的曲线3
区域Ⅱ
```

显式编号示例：

```yaml
- id: asset_reference.explicit_figure
  description: 显式图编号
  enabled: true
  priority: 120
  matcher:
    type: regex
    pattern: '图\s*(\d+)\s*[-—－]\s*(\d+)'
    flags: []
  conditions:
    unit_types: [passage, caption]
  action:
    type: create_asset_reference_candidate
    target: figure
    capture_groups:
      chapter: 1
      sequence: 2
  confidence: 0.99
  tags: [asset]
```

相对指代示例：

```yaml
- id: asset_reference.relative_figure
  description: 上图或下图等相对引用
  enabled: true
  priority: 70
  matcher:
    type: keywords
    mode: any
    values: [上图, 下图, 如图所示, 见图]
  conditions:
    unit_types: [passage]
  action:
    type: create_asset_reference_candidate
    target: figure
  confidence: 0.65
  tags: [asset, ambiguous]
```

相对引用不得直接创建 `MENTIONS` 确定关系，只能创建候选。

### 5.4 跨页连续格式 `continuation`

该组主要使用处理器：

```yaml
- id: continuation.cross_page_sentence
  description: 前页末尾和后页开头组成同一句
  enabled: true
  priority: 100
  matcher:
    type: handler
    name: detect_cross_page_continuation
  conditions:
    unit_types: [passage]
  action:
    type: create_relation_candidate
    target: CONTINUES
  confidence: 0.80
  tags: [cleaning, structure]
```

处理器综合判断：

- 前一段是否缺少终止标点；
- 后一段是否以连接词、谓语或补足成分开头；
- 两个块是否位于相邻页；
- 是否存在标题、图题或表题阻断；
- 字体和段落缩进是否连续。

### 5.5 条件语言 `condition_language`

包括：

```text
在……条件下
当……时
对于……
采用……
其他条件相同
铁锈量为0.5g
保护气体为80%Ar+20%CO₂
```

示例：

```yaml
- id: condition.explicit_under
  description: 在某条件下
  enabled: true
  priority: 90
  matcher:
    type: regex
    pattern: '在(?P<condition>[^，。；]{1,80})条件下'
    flags: []
  conditions:
    unit_types: [title, passage, table_cell, caption]
  action:
    type: create_constraint_candidate
    target: explicit_condition
  confidence: 0.85
  tags: [condition]
```

规则只截取条件候选文本，不直接确定条件类型、规范值和作用范围。

### 5.6 规则语言 `rule_language`

包括：

```text
随着X增加，Y增大
X越大，Y越小
X对Y有显著影响
与A相比，B更高
X可防止Y
X由式（5-21）计算
```

示例：

```yaml
- id: rule.monotonic_change
  description: 随着因素变化而结果变化
  enabled: true
  priority: 90
  matcher:
    type: template
    pattern: '随着{FACTOR}{FACTOR_CHANGE}，{RESULT}{RESULT_CHANGE}'
    slots:
      FACTOR_CHANGE: [增加, 增大, 提高, 减少, 减小, 降低]
      RESULT_CHANGE: [增加, 增大, 提高, 减少, 减小, 降低]
  conditions:
    unit_types: [passage, table_cell, caption]
  action:
    type: add_role_candidate
    target: rule
  confidence: 0.85
  tags: [routing, rule]
```

该匹配只能将内容标记为规则候选，不能直接创建知识关系。

### 5.7 本体语言 `ontology_language`

包括：

```text
X是……
X称为……
X分为A和B
X由A、B组成
A属于X
X包括……
```

示例：

```yaml
- id: ontology.classification
  description: 分类表达
  enabled: true
  priority: 90
  matcher:
    type: regex
    pattern: '(?P<subject>[^，。；]{1,40})(?:可|可以)?分为(?P<classes>[^。；]{1,100})'
    flags: []
  conditions:
    unit_types: [passage, caption]
  action:
    type: add_role_candidate
    target: ontology
  confidence: 0.90
  tags: [routing, ontology]
```

本体和规则允许同时命中，不采用互斥分类。

## 6. Action注册与执行边界

配置文件只能使用代码中注册的Action：

```text
add_role_candidate
add_candidate
create_relation_candidate
create_asset_reference_candidate
create_constraint_candidate
mark_noise_candidate
```

Action只产生候选记录，不能执行跨阶段业务逻辑。

例如：

```text
create_asset_reference_candidate
```

只能记录：

```json
{
  "pattern_id": "asset_reference.explicit_figure",
  "asset_type": "figure",
  "reference_text": "图5-18",
  "normalized_label": "5-18",
  "confidence": 0.99
}
```

它不能直接：

- 搜索整篇文档；
- 强制选择某个资产；
- 创建资产子结构；
- 传播条件；
- 生成上下文包。

这些工作必须由相应业务模块完成。

## 7. 多规则命中和冲突处理

同一Unit可以同时命中多条规则。

例如：

```text
在其他条件相同时，随着焊接电流增加，熔深增大。
```

可以同时命中：

```text
condition.other_conditions_equal
rule.monotonic_change
```

匹配结果全部保留，不因优先级只保留一条。

`priority`主要用于：

- 同一规则组内候选排序；
- 确定规则和宽泛规则冲突时优先采用更具体规则；
- 噪声判断中的排除顺序；
- 多个格式对同一字段给出不同解析时排序。

出现矛盾时不得仅依据优先级删除低优先级结果，应记录：

```text
pattern_conflict
```

并交给业务算法或模型处理。

## 8. 匹配结果保存

每个Unit的匹配结果保存在：

```python
Unit.attributes["pattern_matches"]
```

建议结构：

```json
[
  {
    "pattern_id": "condition.explicit_under",
    "group": "condition_language",
    "matched_text": "在其他条件相同时",
    "span": [0, 10],
    "captures": {
      "condition": "其他条件相同"
    },
    "action": "create_constraint_candidate",
    "confidence": 0.85,
    "config_version": 1
  }
]
```

必须保存：

- 规则ID；
- 规则组；
- 命中文本；
- 字符位置；
- 捕获字段；
- Action；
- 置信度；
- 配置版本。

这样才能核验某个标题、条件或规则候选为什么产生。

## 9. PatternEngine接口

```python
class PatternEngine:
    def match(
        self,
        group: str,
        unit: Unit,
    ) -> list[PatternMatch]:
        ...

    def match_all(
        self,
        unit: Unit,
    ) -> list[PatternMatch]:
        ...

    def explain(
        self,
        unit: Unit,
    ) -> list[PatternMatch]:
        ...

    def validate(self) -> list[PatternValidationError]:
        ...
```

加载入口：

```python
def load_pattern_engine(
    config_path: str,
) -> PatternEngine:
    ...
```

`validate()`至少检查：

- 规则ID是否重复；
- 规则组是否合法；
- 正则是否能够编译；
- handler是否已经注册；
- action是否已经注册；
- `priority`和`confidence`是否合法；
- 模板槽位是否完整；
- 配置字段是否缺失。

## 10. 各模块使用范围

| 规则组 | 使用模块 | 允许产生的结果 |
|---|---|---|
| `noise` | `cleaning.py` | 噪声候选 |
| `heading` | `structure.py` | 标题及等级候选 |
| `asset_reference` | `assets.py` | 资产引用候选 |
| `continuation` | `cleaning.py`、`structure.py` | 跨页连续候选 |
| `condition_language` | `conditions.py` | 条件候选 |
| `rule_language` | `routing.py` | 规则角色候选 |
| `ontology_language` | `routing.py` | 本体角色候选 |

业务模块负责将格式候选与文档结构、位置、资产和模型结果结合，生成最终结果。

## 11. 新增和修改格式的流程

新增格式时：

1. 在相应规则组中增加规则；
2. 使用新的稳定ID；
3. 增加正例；
4. 增加容易误判的反例；
5. 执行配置校验；
6. 执行单条文本测试；
7. 执行对应模块回归测试；
8. 查看命中数量和变化范围。

推荐命令：

```bash
python scripts/inspect_patterns.py validate

python scripts/inspect_patterns.py test \
  --group condition_language \
  --text "在其他条件相同时，随着焊接电流增加"

pytest tests/context_builder/test_patterns.py
```

修改已有规则语义时，应提高顶层版本或建立新规则ID。只修改错别字、说明文字时，可以保留原ID。

## 12. 格式规则测试数据

每条规则至少包含：

- 一个标准正例；
- 一个包含OCR空格或全半角差异的正例；
- 一个相似但不应命中的反例；
- 一个可能与其他规则同时命中的例子。

例如标题规则：

```text
正例：5.2.3 焊接电流
变体：5．2．3　焊接电流
反例：焊接电流为5.2 A
```

资产引用规则：

```text
正例：如图5-18所示
变体：如图 5—18 所示
反例：试验结果见图，但原文未给出图号
```

反例不代表完全不处理，而是不能被“显式编号规则”错误识别。

因此，答案是：**原任务书不是完全没有格式匹配说明，而是只写到了目录和接口层面，尚不足以约束后续实现。**上面这一章应加入任务书，并放在“六个模块”之后、“大语言模型调用”之前。