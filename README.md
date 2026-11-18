# Campus Checkout Workflow

一个用 Python 实现的校园外卖结账模拟器。它把订单和付款分成两条状态线，用同一个事件 ID 验证重复回调、错序重试和冲突。这个独立代码样例源自校园外卖 UML 课程设计，由 Antigravity 辅助生成并经过复核。

A dependency-free Python simulation that models payment and vendor decisions as independent states. It demonstrates exact event replay, out-of-order recovery, and conflict detection.

## 运行

在仓库根目录使用 Python 3.10 或以上版本。

```sh
python3 order_flow.py
python3 -m unittest -v
```

演示输出三条路径：商家接单、错序拒单重试后退款、付款前取消。测试覆盖历史快照、事件重放与 ID 冲突。

## 状态转换

| 当前订单 / 付款 | 事件 | 下一状态 |
| --- | --- | --- |
| `PENDING_PAYMENT` / `UNPAID` | 付款成功 | `AWAITING_VENDOR` / `PAID` |
| `PENDING_PAYMENT` / `UNPAID` | 付款前取消 | `CANCELLED` / `CANCELLED` |
| `AWAITING_VENDOR` / `PAID` | 商家接单 | `ACCEPTED` / `PAID` |
| `AWAITING_VENDOR` / `PAID` | 商家拒单 | `REJECTED` / `REFUND_PENDING` |
| `REJECTED` / `REFUND_PENDING` | 退款确认 | `REJECTED` / `REFUNDED` |

`Order.apply(event)` 对同 ID、同类型和同内容的重放返回当前快照。ID 已使用但内容不同则抛出 `EventConflictError`。状态转换失败时，版本和状态都保持原样，事件 ID 仍可重试。例如，系统可在付款补到后重新处理此前错序送达的拒单事件。

事件载荷使用字符串键，值可为字符串或整数。处理记录在单进程内按订单隔离，调用方负责按序处理同一订单。付款和退款由本地事件模拟，状态会随进程退出而清空。
