# 每日运行手册

本文面向运营和执行同事，目标是稳定完成每日 H5 巡检。

## 1. 每天运行前检查

确认：

- 手机电量充足。
- 手机未锁屏。
- 手机网络稳定。
- Mac 能识别 adb 设备。
- 朴朴 App 可正常打开。
- `config/cities.json` 和 `config/brands.json` 的 enabled 配置正确。
- 每个 enabled 城市配置了 `address_candidates`，并优先使用市中心大型商圈 / 地铁站，其次市政府 / 区政府、三甲医院 / 大学 / 大型公园、大型购物中心、派出所 / 公安局。

## 2. 手机和网络检查

建议：

- 使用稳定 Wi-Fi。
- 关闭不必要弹窗。
- 不要人工触碰手机。
- 不要在采集中切 App。

## 3. 环境校验命令

启动 Appium：

```bash
bash tools/start_appium_server.sh
```

环境检查：

```bash
python3 run_mvp.py --doctor
```

查看计划：

```bash
python3 run_mvp.py --plan --city 福州市 --brand 卫龙
```

## 4. 采集命令

单样本采集：

```bash
python3 run_mvp.py --capture-one --city 福州市 --brand 卫龙 --date YYYY-MM-DD --strict --fast --overwrite --output-mode h5 --debug-recommendation
```

全量采集应按项目当前脚本能力和用户授权执行。不要在未授权时覆盖已有成功样本。

采集完成后先做地址门禁，不通过时不要进入 cleaned、gatekeeper 或交付：

- `selected_address_text` 必须非空。
- `delivery_available` 必须为 true。
- `city_switch_verified` 必须为 true。
- 如 `address_candidate_status=city_address_unavailable`，先调整该城市地址候选池。

## 5. 离线分析命令

单样本：

```bash
python3 tools/h5_delivery_postprocess.py --date YYYY-MM-DD --brand 卫龙 --city 福州市
```

全量：

```bash
python3 tools/h5_delivery_postprocess.py --date YYYY-MM-DD --all
```

## 6. cleaned 重建命令

```bash
python3 tools/h5_delivery_postprocess.py --date YYYY-MM-DD --generate-cleaned
```

只会生成 `delivery_grade=fixable` 的 cleaned 图；`delivery_grade=pass` 表示原始 H5 已可用，无需 cleaned，也应计入通过门禁。

cleaned 重建前必须确认地址门禁已通过；未验证地址或不可配送地址的样本不能 cleaned。

## 7. cleaned 质检命令

当前质检报告由项目流程生成：

- `_cleaned_quality_check.csv`
- `_cleaned_quality_check.md`
- `_cleaned_quality_check.json`

如需新增正式 CLI，应先作为单独小任务实现，不要混在采集或清洗规则修改中。

## 8. gatekeeper 命令

gatekeeper 当前输出：

- `_delivery_gatekeeper_report.csv`
- `_delivery_gatekeeper_report.md`
- `_delivery_gatekeeper_report.json`

门禁检查必须动态读取配置计算 expected_total。

gatekeeper 前必须先过地址门禁：所有城市 `selected_address_text` 非空、`delivery_available=true`、`city_switch_verified=true`。任一城市不满足，都禁止进入 gatekeeper 和交付包。

## 9. gate pass 后是否生成交付包

gate pass 后只是允许发布，不等于必须立刻发布。

只有用户明确说“生成交付包”或等价指令时，才生成 `delivery_packages/YYYY-MM-DD/`。

当前本地品牌 ZIP 打包命令：

```bash
python3 tools/package_daily_delivery.py --date YYYY-MM-DD
```

默认不覆盖已有 ZIP；如确需重打同日包：

```bash
python3 tools/package_daily_delivery.py --date YYYY-MM-DD --overwrite-package
```

输出目录：

- `delivery_packages/YYYY-MM-DD/by_brand/`
- `delivery_packages/YYYY-MM-DD/manifest/`

命名规则：

- ZIP：`品牌巡价YYYY年M月D日.zip`
- ZIP 内图片：`品牌-城市-YYYY.MM.DD.png`，城市名去掉“市”

注意：

- 必须 gatekeeper pass 才能打包。
- 只允许使用 `deliverables_h5_cleaned/` 作为交付源。
- 当前尚未接飞书上传，下一步才是飞书云盘上传。

## 10. gate fail 后怎么处理

按 gate 报告里的 `action_required` 处理：

- batch 行数不对：重跑全量离线分析。
- cleaned 缺图：重建对应 cleaned。
- quality warning/fail：先定位图或规则问题。
- rerun_required 非空：先补跑或人工复核。
- possible_missing_sku 非 0：不能交付。

## 11. 常见失败

`search_failed`

- 搜索框输入或提交失败。
- 先检查 App 状态和弹窗。

`city_failed`

- 切城或地址选择失败。
- 检查 `address_candidates` 是否都失败，优先替换为市中心商圈 / 地铁站、市政府、医院 / 大学 / 公园、大型购物中心、派出所 / 公安局。

`city_address_unavailable`

- 所有地址候选都不可用或无法验证。
- 不进入 cleaned，先补充该城市候选地址。

`address_delivery_unavailable`

- 当前候选地址提示暂未开通、超出配送范围或无法配送。
- 换下一个候选，全部失败则记录城市不可用。

`selected_address_text_unavailable`

- 城市可能已切换，但无法读取实际选中地址。
- 不允许交付，先修地址记录或人工复核。

`app_failed`

- Appium 或 App 前台状态异常。
- 重启 Appium 和朴朴 App。

`invalid_result_page`

- 结果页不是标准搜索结果。
- 查看失败截图和 OCR 日志。

`possible_missing_sku`

- 存在不完整卡且找不到完整对应版本。
- 不能交付，必须进入 bug 闭环或人工复核。

`detail_page_warning`

- 疑似采到了商品详情页。
- 必须 fail，不允许强行 cleaned。

`product_detail_page_misclassified`

- 详情页里的相似商品被误当主列表。
- 需要重新采集该城市/品牌搜索结果页。

## 12. 失败补跑原则

- 只补跑失败样本。
- 不全量重跑成功样本，除非用户授权。
- 不覆盖其他城市/品牌。
- 补跑后只对该样本离线分析。
- 确认修复后再全量离线分析。

## 13. 人工复核地址口径

朴朴可能按“城市 + 具体收货地址 / 前置仓”展示不同 SKU、价格、活动和库存。

人工复核前必须先看 capture summary：

- `target_city`
- `address_keyword`
- `address_candidates`
- `attempted_address_candidates`
- `selected_address_keyword`
- `selected_address_text`
- `delivery_available`
- `address_match_warning`

人工手机必须使用脚本同一个城市和同一个收货地址。若人工地址不同，只能记录 `manual_review_address_mismatch_possible`，不能直接判定“脚本漏 SKU”或“清洗误删”。

## 14. 不允许人工硬放行的情况

- gatekeeper 不通过。
- rerun_required 非空。
- possible_missing_sku 非 0。
- cleaned quality 有 warning 或 fail。
- 商品详情页误入。
- 推荐区商品混入 cleaned。
- 有效主列表 SKU 被误删且未修复。
- cleaned manifest 缺业务统计字段。
