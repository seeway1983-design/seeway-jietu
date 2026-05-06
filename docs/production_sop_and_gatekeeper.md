# 生产 SOP 与 Gatekeeper

本文定义朴朴巡价 H5 生产流程和总门禁。所有数量必须动态计算：

```text
enabled_city_count = config/cities.json 中 enabled=true 的城市数量
enabled_brand_count = config/brands.json 中 enabled=true 的品牌数量
expected_total = enabled_city_count × enabled_brand_count
```

禁止写死城市数、品牌数或总样本数。

## 1. 配置读取

输入规范：

- `config/cities.json`
- `config/brands.json`
- 只读取 `enabled=true` 的城市和品牌。

输出规范：

- `enabled_city_count`
- `enabled_brand_count`
- `expected_total`
- enabled 城市列表和品牌列表。

验收门禁：

- 城市和品牌不能为空。
- expected_total 必须大于 0。

不通过时怎么处理：

- 检查 JSON 格式和 `enabled` 字段。
- 不要用手写数量绕过配置。

升级 / 回退条件：

- 如当天只跑子集，必须由用户明确授权，并记录不是生产全量。

地址候选配置：

- `address_candidates` 优先于单个 `address_keyword`，但保留 `address_keyword` 作为兼容字段和默认首选候选。
- 候选优先级建议：市中心大型商圈 / 地铁站；市政府 / 区政府；三甲医院 / 大学 / 大型公园；大型购物中心；派出所 / 公安局。
- 同一城市候选地址应按稳定、可配送、便于人工复核排序。

## 2. 环境校验

输入规范：

- Android 真机已连接。
- Appium 可启动。
- 朴朴 App 已安装。

输出规范：

- doctor 检查结果。
- Appium 服务状态。

验收门禁：

- `python3 run_mvp.py --doctor` 通过。
- Appium `http://127.0.0.1:4723` 可用。

不通过时怎么处理：

- 重新连接手机。
- 重启 Appium。
- 检查 adb serial。

升级 / 回退条件：

- Appium 连续不可用时暂停采集，保留环境错误，不切换到暂停路线。

## 3. 采集计划

输入规范：

- enabled 城市。
- enabled 品牌。
- 目标日期。

输出规范：

- 城市 × 品牌执行计划。

验收门禁：

- 计划样本数等于 expected_total。

不通过时怎么处理：

- 回查配置，不要手工补数字。

升级 / 回退条件：

- 点位失效时优先补充或调整该城市 `address_candidates`，不要回退为单个不稳定地址硬跑。

## 4. 原始 H5 采集

输入规范：

- 目标城市、品牌、日期。
- `--output-mode h5`。
- 建议使用 `--strict --fast --debug-recommendation`。

输出规范：

- `deliverables_h5/品牌/日期/城市/品牌（城市 日期）H5长图.png`
- `debug/h5_pages/日期/品牌/城市/`
- capture summary。
- capture summary 必须记录 `target_city`、`address_keyword`、`address_candidates`、`attempted_address_candidates`、`selected_address_keyword`、`selected_address_text`、`address_candidate_status`、`address_page_confirmed`、`city_switch_verified`、`selected_city_verified`、`delivery_available`、`address_match_warning`。

验收门禁：

- 采集状态 success。
- `selected_address_text` 非空。
- `delivery_available=true`。
- `city_switch_verified=true`。
- 不是商品详情页。
- 不是首页、活动页、空结果页。
- 人工复核前必须确认人工手机使用的城市和收货地址，与脚本 summary 中记录的地址一致。

不通过时怎么处理：

- 只补跑失败样本。
- 不覆盖其他城市/品牌。
- 如果人工地址与脚本地址不一致，只能记录 `manual_review_address_mismatch_possible`，不能直接判定为漏 SKU 或误删。
- 如果出现 `city_address_unavailable`、`address_delivery_unavailable`、`city_switch_unverified` 或 `selected_address_text_unavailable`，先修地址候选，不进入 cleaned。

升级 / 回退条件：

- 连续进入详情页或推荐页时，暂停该样本并进入 bug 闭环。
- 所有候选地址都显示暂未开通、超出配送范围或无法配送时，该城市当天进入人工地址复核，不强行交付。

## 5. 失败补跑

输入规范：

- capture summary。
- failed sample 列表。

输出规范：

- 补跑后的原始 H5 和分页图。

验收门禁：

- 只补跑失败项。
- 不重跑成功项，除非用户授权。

不通过时怎么处理：

- 记录 rerun_required。

升级 / 回退条件：

- 同一城市/品牌多次失败时进入人工复核。

## 6. 离线分析

输入规范：

- `debug/h5_pages/日期/品牌/城市/`
- `deliverables_h5/`

输出规范：

- `reports/日期/h5_cleaning/品牌/城市.json`
- `reports/日期/h5_cleaning/品牌/城市.csv`
- `reports/日期/h5_cleaning/品牌/城市.md`
- rebuilt 图。
- `_batch_summary.csv/json`

验收门禁：

- batch summary 行数等于 expected_total。
- 地址门禁已通过：所有城市 `selected_address_text` 非空、`delivery_available=true`、`city_switch_verified=true`。
- 不允许 `delivery_grade=fail` 进入 cleaned。
- `possible_missing_sku=0`。

不通过时怎么处理：

- 一处 bug 一处验证。
- 先单样本，后回归样本，再全量离线分析。

升级 / 回退条件：

- 商品详情页误入必须 fail。
- 找不到完整版本的不完整 SKU 不能静默删除。

## 7. bug 闭环

输入规范：

- 单个明确 bug_id。
- 具体品牌、城市、card_id、preview。

输出规范：

- 修改点。
- 单样本复测。
- 横向样本回归。
- 是否可进入全量离线分析。

验收门禁：

- 不因 grade 好看而硬删。
- 不破坏推荐区排除。
- 不隐藏 possible_missing_sku。

不通过时怎么处理：

- 保持 fail 或 warning，不强行 cleaned。

升级 / 回退条件：

- 规则影响多个品牌时必须加横向回归样本。

## 8. cleaned 重建

输入规范：

- `_batch_summary.csv/json`
- 每个 fixable 样本的 rebuilt 图。

输出规范：

- `deliverables_h5_cleaned/品牌/日期/城市/品牌（城市 日期）H5清洗图.png`
- `_cleaned_manifest.csv/md`

验收门禁：

- 进入 cleaned 前必须先过地址门禁：`selected_address_text` 非空、`delivery_available=true`、`city_switch_verified=true`。
- `delivery_grade=pass` 通过清洗门禁，表示原始 H5 已可用，无需 cleaned。
- `delivery_grade=fixable` 通过清洗门禁，但必须生成 cleaned 且 quality pass。
- 不处理 fail。
- 不覆盖原始 `deliverables_h5/`。

不通过时怎么处理：

- rebuilt 缺失则记录 failed。
- 不把 fail 强行放进 cleaned。

升级 / 回退条件：

- cleaned 失败数不为 0 时不能进入 gatekeeper pass。

## 9. cleaned 质检

输入规范：

- `_cleaned_manifest.csv`
- `deliverables_h5_cleaned/`

输出规范：

- `_cleaned_quality_check.csv`
- `_cleaned_quality_check.md`
- `_cleaned_quality_check.json`

验收门禁：

- 检查数等于 expected_total。
- 全部 `quality_result=pass`。
- warning=0。
- fail=0。
- 文件存在，大小不为 0。
- 路径都在 `deliverables_h5_cleaned/`。

不通过时怎么处理：

- 缺图就重建对应 cleaned。
- 尺寸异常先判断是否真实 SKU 很少，不能无证据放行。

升级 / 回退条件：

- 业务复核发现误删/误收时回到 bug 闭环。

## 10. rerun_required 检查

输入规范：

- `_rerun_required.csv/md`

输出规范：

- 空清单或失败样本清单。

验收门禁：

- 生产放行时必须为空。

不通过时怎么处理：

- 逐个补跑或修复。

升级 / 回退条件：

- 不允许带 rerun_required 生成交付包。

## 11. gatekeeper 总门禁

输入规范：

- 配置文件。
- batch summary。
- cleaned manifest。
- quality check。
- rerun_required。
- `deliverables_h5_cleaned/`。

输出规范：

- `_delivery_gatekeeper_report.csv`
- `_delivery_gatekeeper_report.md`
- `_delivery_gatekeeper_report.json`

验收门禁：

- 地址门禁通过。
- overall_gate_result 必须为 pass。
- 所有 gate 必须 pass。

不通过时怎么处理：

- 按 gate 的 `action_required` 修复。
- 修复后重新跑相关环节。

升级 / 回退条件：

- gatekeeper 不通过不能生成交付包。

## 12. 交付包生成

输入规范：

- gatekeeper pass。
- cleaned H5，来源必须在 `deliverables_h5_cleaned/`。
- 用户明确授权发布。
- 当前阶段只生成本地品牌 ZIP，不接飞书上传。

输出规范：

- `delivery_packages/YYYY-MM-DD/by_brand/品牌巡价YYYY年M月D日.zip`
- `delivery_packages/YYYY-MM-DD/manifest/delivery_manifest.csv`
- `delivery_packages/YYYY-MM-DD/manifest/delivery_manifest.md`
- `delivery_packages/YYYY-MM-DD/manifest/package_summary.json`

执行命令：

```bash
python3 tools/package_daily_delivery.py --date YYYY-MM-DD
```

如需覆盖已存在的同日 ZIP，必须显式传入：

```bash
python3 tools/package_daily_delivery.py --date YYYY-MM-DD --overwrite-package
```

命名规范：

- 品牌 ZIP：`品牌巡价YYYY年M月D日.zip`。
- ZIP 内图片：`品牌-城市-YYYY.MM.DD.png`，城市名去掉“市”。

验收门禁：

- 数量符合 expected_total。
- 不包含 fail。
- 不包含 rerun_required。
- 每个品牌 ZIP 内图片数量等于 enabled_city_count。
- 总 manifest 行数等于 expected_total。
- 不使用 `deliverables_h5/`、`screenshots_failed/`、`debug/` 或 reports 中间预览图作为交付源。

不通过时怎么处理：

- gatekeeper 不通过时不生成 ZIP，只记录 `blocked_by_gatekeeper`。
- 缺失 cleaned 图时不生成对应品牌 ZIP，manifest 记录 `missing_cleaned_image`。
- 回到 gatekeeper 前的失败环节修复，不手工拼包。

升级 / 回退条件：

- 发布动作必须可审计，不覆盖历史包。
- 飞书云盘上传是下一阶段能力，本地 ZIP 通过后再接入。

## 13. 归档

输入规范：

- 原始 H5。
- cleaned H5。
- reports。
- gatekeeper。
- delivery package。

输出规范：

- 日期维度归档完整。

验收门禁：

- 原始、cleaned、reports、delivery package 边界清晰。

不通过时怎么处理：

- 补齐缺失报告，不补写业务结果。

升级 / 回退条件：

- 缺审计报告时暂停交付。
