# 储能柜荷电与质保循环材料

本目录是新增两份数据产品（`soc-report/v1` 与 `warranty-report/v1`）的可重跑示例。
所有计算使用精确有理数；时间单位为任意“时段”，功率×时段即库存侧电量。

| 文件 | 演示点 | 关键结论 |
|------|--------|----------|
| `worked.json` | 题面四个单位时段净功率 +6、−4、+2、−4，容量 6、起始 0、效率 1 | 转折值 `0,6,2,4,0`；深度 2 整循环 1 次 + 深度 6 半循环 2 次；合同 w1 耗用 4，浅循环免费的 w2 耗用 3 |
| `shortfall.json` | 同上但第三段实为 0 | 转折值 `0,6,0`；末段 2 单位未供；原合同下也只耗 3 |
| `saturation.json` | 段内饱和 | 容量 5，t=11/9 到顶，溢出 16 |
| `gap_anchor.json` | 缺测后由带时间的独立荷电锚点重启 | 历史截断为两段独立 run：`[0,2)` 与 `[4,6)`；缺口 `[2,4)` 不拼接 |

运行（二选一）：

```bash
python3 -m power_billing.cli demo                               # 一次性跑完所有示例
python3 -m power_billing.cli warranty --scenario materials/soc/worked.json
python3 -m power_billing.cli soc      --scenario materials/soc/gap_anchor.json
```

`warranty --scenario` 会读取 `contracts` 数组并逐一核算（相当于切换合同）；
也可用 `--contracts a.json b.json` 显式指定。`soc --scenario` 若含 `revise`
块，会在重建前先注册修订后的轨迹，并打印“修订前 / 修订后”两份报告（旧报告仍可按
原 `report_id` 取回）。

## 场景文件字段

```jsonc
{
  "charge":    {"trace_id": "C", "revision": "r1", "segments": [...]},  // 充入非负功率
  "discharge": {"trace_id": "D", "revision": "r1", "segments": [...]},  // 取出非负功率
  "params":    {"initial_charge": 0, "capacity": 6,
                "charge_efficiency": 1, "discharge_efficiency": 1},
  "anchors":   [{"time": 4, "charge": 1}],        // 可选：重启 run 的实测锚点
  "contracts": [ {"contract_id": "W", "revision": "w1",
                  "tiers": [{"max_depth": 2, "full_cycle_cost": 1}, ...]} ],
  "revise":    {"trace": "charge", "revision": "r2", "segments": [...]}   // 可选
}
```

约定：**某时段两路数据都已知才算“已知”**；任一路缺段即未知（不补零、不外推）。
未知段截断可证明历史；只有 `anchors` 中时间与已知数据重启点重合的独立荷电实测能把
后续时段重新起段。缺口两侧绝不拼成一个循环。
