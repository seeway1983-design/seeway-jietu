# 当前架构

## 数据流

```text
config/cities.json
config/brands.json
  -> run_mvp.py
  -> Android 真机 + 朴朴 App
  -> debug/h5_pages/
  -> deliverables_h5/
  -> tools/h5_delivery_postprocess.py
  -> reports/YYYY-MM-DD/h5_cleaning/
  -> deliverables_h5_cleaned/
  -> gatekeeper
  -> delivery_packages/
```

## 控制流

1. 读取 enabled 城市和品牌。
2. 按城市/品牌执行采集。
3. 产出原始 H5 和高重叠分页图。
4. 离线分析分页图中的商品卡。
5. 标记完整卡、不完整卡、推荐区卡、重复 SKU、残片重复。
6. 重建 cleaned H5。
7. 执行 cleaned quality check。
8. 执行 gatekeeper。
9. 用户确认后进入交付包发布。

## 从配置到 H5 输出

`config/cities.json` 控制城市范围，`config/brands.json` 控制品牌范围。生产数量必须动态计算：

```text
expected_total = enabled_city_count × enabled_brand_count
```

采集入口是 `run_mvp.py`。目标输出包括：

- 分页图：`debug/h5_pages/日期/品牌/城市/`
- 原始 H5：`deliverables_h5/品牌/日期/城市/`
- 采集 summary：`reports/日期/`

## 原始 H5 与 cleaned H5 的关系

原始 H5：

- 是采集侧产物。
- 用于留档和复盘。
- 可能有断层、重复、推荐区污染或详情页误入。

cleaned H5：

- 是离线清洗后的交付候选。
- 基于高重叠分页截图和卡片级分析重建。
- 只来源于 `delivery_grade=fixable` 且 rebuilt 存在的样本。
- `delivery_grade=pass` 不生成 cleaned，表示原始 H5 已通过清洗门禁。

## reports 的作用

`reports/YYYY-MM-DD/h5_cleaning/` 是项目审计中心，包含：

- 单样本 JSON/CSV/Markdown。
- 商品卡 preview。
- `_batch_summary.csv/json`。
- `_cleaned_manifest.csv/md`。
- `_cleaned_quality_check.csv/md/json`。
- `_rerun_required.csv/md`。
- `_delivery_gatekeeper_report.csv/md/json`。

## gatekeeper 的作用

gatekeeper 是交付包前的总门禁，负责把配置数量、batch summary、cleaned manifest、quality check、rerun_required 和实际文件统一核对。

gatekeeper 通过才允许发布。

## delivery_packages 的作用

`delivery_packages/` 是发布结果，不是开发过程产物。只有用户确认发布时才生成。

交付包应包含：

- 按品牌整理的 cleaned H5。
- delivery manifest。
- 必要的 summary 或 gatekeeper 证明。

## 过程产物目录

- `debug/h5_pages/`
- `reports/`
- `logs/`
- `screenshots_failed/`
- `tmp_review/`

## 交付候选目录

- `deliverables_h5_cleaned/`

## 原始留档目录

- `deliverables_h5/`

## 失败和调试证据

- `screenshots_failed/`
- `reports/YYYY-MM-DD/*_capture_summary.csv/json`
- `reports/YYYY-MM-DD/h5_cleaning/品牌/城市.md`
- `reports/YYYY-MM-DD/h5_cleaning/品牌/城市/page_xx/card_xx.png`
- `_rerun_required.csv/md`
