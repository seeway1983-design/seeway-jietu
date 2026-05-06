# H5 长图最小改动落地方案

本文档描述如何在不新增截图模式、不抓接口、不重构主链路的前提下，把 H5 长图的“去重 + 去截断”清洗能力增量落到当前项目。

## 1. 目标

在现有链路基础上，新增一层“交付清洗”：

- 输入仍然是当前 `debug/h5_pages/` 和 `deliverables_h5/`
- 输出仍然是当前 `deliverables_h5/品牌/日期/城市/品牌（城市 日期）H5长图.png`
- 不改变城市切换、搜索、截图、归档主流程
- 不覆盖原始截图页和历史产物

新增能力目标：

- 识别重复 SKU 卡片
- 删除被完整版本覆盖的截断卡片
- 标记不可自动处理的异常图
- 把清洗结果写入 summary 与交付 manifest

## 2. 现状基础

当前项目已具备可复用输入和信号：

- 原始高重叠分页截图：`debug/h5_pages/YYYY-MM-DD/品牌/城市/`
- 正式 H5 图：`deliverables_h5/品牌/YYYY-MM-DD/城市/`
- 每日 summary：`reports/YYYY-MM-DD/all_brands_capture_summary.csv`
- 现有字段已包含：
  - `page_count`
  - `viewport_count`
  - `overlap_ratio`
  - `transition_warnings`
  - `duplicate_overlap_kept`
  - `reached_recommendation_section`
  - `maybe_truncated`
  - `h5_image_path`

当前主执行脚本：

- `tools/run_daily_real_device_capture.py`

## 3. 设计原则

最小改动方案只做“后处理清洗”，不入侵采集过程。

具体原则：

- 不改截图模式
- 不改滚动逻辑
- 不改 Appium 导航流程
- 不改原始分页截图目录结构
- 不覆盖已有 H5 原图，必要时先备份同城原始 H5

推荐做法：

- 在 H5 图生成后，增加一个 `post_process_h5_delivery()` 阶段
- 清洗只针对“当天、当前品牌、当前城市”的产物

## 4. 建议落点

建议新增一个独立模块，而不是把大量逻辑塞进主流程文件。

推荐新增：

- `tools/h5_delivery_postprocess.py`

该模块职责：

- 读取 `debug/h5_pages` 页图
- 从页图提取候选商品卡片
- 在全图范围内做 SKU 匹配
- 生成清洗决策
- 输出清洗后的 H5 成图
- 输出结构化清洗结果

主流程只做一件事：

- 在 H5 图产出后调用这个模块

这样改动最小，也最容易回滚。

## 5. 输入与输出

输入：

- 当前城市品牌的 `debug/h5_pages/.../*.png`
- 当前城市品牌的正式 H5 图
- 当前次执行上下文：品牌、城市、日期、页数

输出建议：

- 清洗后的正式 H5 图，仍写回既有 `deliverables_h5` 路径
- 原始未清洗 H5 备份到新目录
- 一份清洗元数据 JSON

建议新增目录：

```text
deliverables_h5_raw/
  品牌/
    YYYY-MM-DD/
      城市/
        品牌（城市 日期）H5长图.raw.png

reports/YYYY-MM-DD/h5_cleaning/
  品牌/
    城市.json
```

说明：

- `deliverables_h5_raw/` 只用于保底，不替代正式交付目录
- 如不想长期保留，也可只在清洗发生时才备份

## 6. 最小实现流程

建议分成 6 个步骤。

### 步骤 1：卡片候选提取

基于 `debug/h5_pages` 的每一页图做卡片切分，得到候选商品卡片列表。

建议优先使用视觉规则，不依赖接口：

- 利用页面白底卡片块、圆角边界、价格区红色文字、加购按钮等稳定结构
- 记录每个候选卡片的来源页、页内位置、裁切框

输出结构：

- `page_index`
- `card_index`
- `bbox`
- `crop_path`

### 步骤 2：提取卡片特征

对每个候选卡片提取最小特征集：

- `title_text`
- `price_text`
- `spec_text`
- `image_hash`
- `is_truncated_top`
- `is_truncated_bottom`
- `is_occluded`

建议优先顺序：

- 先用本地 OCR 提标题、价格、规格
- 再用简单图片哈希做主图辅助判重

这里不需要追求 100% 识别率，只需要足以支持“强匹配优先”的清洗决策。

### 步骤 3：完整/截断判定

对每个候选卡片打标签：

- `complete`
- `truncated`
- `uncertain`

推荐判据：

- 卡片框碰到页顶或页底，且标题/价格/主图不完整，记为 `truncated`
- OCR 能读出完整标题和价格，且卡片主体完整，记为 `complete`
- 边界或文字不稳定，记为 `uncertain`

### 步骤 4：全图范围同 SKU 聚合

不要只在相邻页之间查重，而是对全体候选卡片做聚合。

推荐聚合键：

- 主键：`normalized_title + normalized_price + normalized_spec`
- 辅键：`image_hash`

聚合结果里，每个 group 对应一个候选 SKU 集合。

### 步骤 5：清洗决策

对每个 SKU group 做决策：

- 若存在 `complete` 卡片，保留一个最佳版本
- 其余 `complete` 重复版本删除
- 同 group 下的 `truncated` 版本全部删除
- 若全 group 只有 `truncated`，则标记 `needs_review`
- 若同标题但规格或价格冲突，则标记 `ambiguous`

建议输出决策字段：

- `sku_group_id`
- `selected_card_id`
- `dropped_card_ids`
- `decision_reason`
- `needs_manual_review`

### 步骤 6：回写交付结果

按决策重建正式 H5 图。

推荐方式：

- 不是在原大图上做局部抹除
- 而是直接按“保留卡片顺序”重新拼一张 H5 交付图

这样更稳，且不容易留下拼接痕迹。

最终写回：

- `deliverables_h5/.../品牌（城市 日期）H5长图.png`

## 7. 为什么建议基于页图重建，而不是直接修补现有大图

直接在现有大图上擦除重复块，风险较高：

- 容易留下空白缝
- 容易误伤上下卡片边缘
- 悬浮按钮、推荐条等遮挡物不好处理

而基于页图抽卡后重建的优点是：

- 每张卡片边界更干净
- 更容易保留一个“最佳版本”
- 同时适配去重和去截断

这仍然属于后处理，不算新增截图模式。

## 8. 与现有 summary 的衔接

建议在当前 summary 基础上新增少量字段，不改老字段语义。

推荐新增：

- `delivery_grade`
- `h5_cleaning_applied`
- `h5_cleaning_reason`
- `h5_raw_backup_path`
- `h5_cleaning_report_path`
- `duplicate_sku_groups`
- `dropped_duplicate_cards`
- `dropped_truncated_cards`
- `needs_manual_review`

字段建议含义：

- `delivery_grade`：`pass` / `fixable` / `fail`
- `h5_cleaning_applied`：是否进行了正式清洗
- `h5_cleaning_reason`：如 `duplicate_cards_removed;truncated_cards_removed`
- `needs_manual_review`：是否存在无法自动确认的风险

## 9. 与交付包的衔接

在 `delivery_packages/YYYY-MM-DD/` 的 manifest 中，建议同步增加以下字段：

- `delivery_grade`
- `h5_cleaning_applied`
- `needs_manual_review`

如果某图被判为 `fail`：

- 不建议默认进入正式交付包
- 或者应在 manifest 中明确标出不可直接交付

如果某图被判为 `fixable`：

- 进入交付包的是清洗后版本
- manifest 记录“已做确定性清洗”

## 10. 人工复核触发条件

以下情况建议强制人工复核：

- 全图存在截断卡片，但找不到完整对应项
- 同标题出现多个价格
- 规格 OCR 不稳定，无法区分 `*3` 与 `*12`
- 推荐区与搜索结果流混杂严重
- 候选卡片切分失败，导致重建顺序不可信

## 11. 建议的迭代顺序

为了风险最小，建议分三步推进。

第一步：

- 只输出清洗分析 JSON，不改正式 H5
- 用 2026-05-04 的 44 张图做离线验证

第二步：

- 开启“备份原 H5 + 输出清洗后 H5”
- 仅对 `delivery_grade=fixable` 的图应用自动清洗

第三步：

- 将清洗结果写入 summary 和 delivery manifest
- 把人工复核口径接入打包流程

## 12. 预计改动范围

新增文件：

- `tools/h5_delivery_postprocess.py`
- 可选：`tools/build_delivery_package.py`

建议改动文件：

- `tools/run_daily_real_device_capture.py`
- `run_mvp.py`

改动方式建议：

- `run_daily_real_device_capture.py` 只负责在 H5 产出后调用后处理函数
- `run_mvp.py` 只负责暴露一个开关，比如 `--clean-h5-delivery`
- 默认可以先关闭，待离线验证通过后再默认开启

## 13. 不建议现在做的事

为了保持最小改动，当前阶段不建议：

- 重写截图主流程
- 引入抓包或接口比对
- 引入复杂训练模型
- 直接改成全新的长图生成方案
- 在原图上做像素级修补

## 14. 成功标准

这个方案落地后，应达到以下结果：

- 类似“同 SKU 重复 + 截断卡片”的 H5 图可以自动清洗
- 像 `500ml*3` 和 `500ml*12` 这种近似 SKU 不会误删
- 原始分页截图和原始 H5 仍可追溯
- 交付包能明确区分 `pass`、`fixable`、`fail`

## 15. 一句话方案

最小改动方案就是：保留现有采集链路不动，在 H5 成图后新增一个基于页图抽卡、全图 SKU 聚合、重建正式交付图的后处理步骤。
