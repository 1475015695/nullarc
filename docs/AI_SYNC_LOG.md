# 铭凡 UM790 Pro 双端 AI 协同与变更记录 (`feat/um790pro`)

> **用途说明**：本文件保存在仓库 `docs/AI_SYNC_LOG.md`，用于在「Windows 开发优化端 AI」与「UM790 Pro macOS 实体机测试端 AI」之间同步最新代码状态、实机构建与验证方法、环境变量/sysctl 控制开关、外接显示合并边界以及实机测试回执模板。
>
> **实体机 AI 使用方式**：拉取 `feat/um790pro` 后请优先通读本文件与 [`docs/UM790PRO_OPTIMIZATION_PLAN.md`](UM790PRO_OPTIMIZATION_PLAN.md)；完成编译与实机测试后，请按第 6 节 `[HW-Report]` 模板整理结果（可直接追加至本文件第 7 节并提交，或交由用户发回）。

---

## 1. 分支与提交记录

- **上游基线**：`luuumi-dev/nullarc`（`main @ 3497d87`）
- **当前分支**：`1475015695/nullarc` → `feat/um790pro`
- **提交历史（按时间倒序）**：
  - `f206737` — `test(um790pro): drop duplicate PHXN_PLACED_HI_POOL and disambiguate nvmtl_vk_cmd signatures`（P1 宿主机 CI 全绿验证，Run [`44475519597`](https://github.com/1475015695/nullarc/actions/runs/44475519597)）
  - `a6fb16f` — `test(um790pro): fix Clang -Werror and C11 harness issues in P1 host tests`
  - `5f2ef85` — `docs(um790pro): add AI_SYNC_LOG handoff record and update P0/P1 progress`
  - `a209038` — `feat(um790pro): P1 CPU load reduction and GPU pipeline optimizations`（P1 全量核心优化与 `test_phx_va_maps.cpp` / `test_native_p1_perf.py` 单测）
  - `83f6770` — `test(um790pro): fix bzero linkage and IOMemoryDescriptor stub in P0 test`（P0 CI 全绿验证，Run [`44448991709`](https://github.com/1475015695/nullarc/actions/runs/44448991709)）
  - `9c5ee04` — `fix(um790pro): resolve kext duplicate helper and host test warnings`
  - `abb662b` — `feat(um790pro): P0 safety baseline and kernel stability fixes`
  - `3986a08` — `docs: add UM790 Pro safety baseline and optimization roadmap`

---

## 2. 并行开发合并硬边界（双方 AI 必须严格遵守）

外接 HDMI / USB4 Type-C DP 显示输出适配正在另一条分支并行开发。为保证后续无痛合入，**本分支严格禁止修改下列路径与符号**（已验证至今改动为 0）：

- `src/amd/phx_dcn*`
- `src/amd/phx_dmub*`
- `src/amd/phx_hdmi*`
- `src/phxaccel/accel/phxfb.cpp` 中的 `PhxHDMIHead`、`PhxHDMINub`、`phxSpawnHdmi`、`phxfb_hdmi_sysctl`

> 主屏 `PhxAMDFB` 的改动仅限 `vblFire` / `displayPower` / `PhxAMDFB::stop` 的原子停止状态与定时器先停后释，未触及任何 HDMI/DP 相关逻辑。

---

## 3. 已完成优化清单（P0 + P1）

### 3.1 P0 — UM790 Pro 安全基线与内核稳定性

| 文件 | 关键符号 / 改动 | 解决的问题 |
| :--- | :--- | :--- |
| `src/phxaccel/accel/nvrm-accel.cpp` | `phxFirebatEcAllowed()`、`fFirebatEcMap`、`IODeviceMemory::withRange(0xFE0B0400, 0x100)`、移除全部 `ml_io_map` | 隔离 Firebat U6C 专属 EC 盲写（UM790 Pro 默认完全禁用），消除 1Hz 泄漏内核 VA 风险并在 `stop()` 清理映射。 |
| `src/phxaccel/accel/nvrm-accel.cpp` | `NVSurfPending::refs`、`nvSurfMark`、`nvSurfWaitReady`、`NVDisplayPipe::performTransaction` | 并发帧引用计数防止提前清标；等待超时时跳过半成品拷贝并递增 `gCntIopCopySkip`、保留面板上一帧。 |
| `src/phxaccel/accel/nvrm-accel.cpp` | `nvAccelIopCacheGet` / `Put` / `Forget`、`NVScopedRetain`、`NVResource::free`、`NVVidMemory::deallocPhysical` / `free` | 修复无 family lock 下 `gIopFlipCache` 悬空指针 UAF；所有 `OSObject::release()` 移至 `IOSimpleLock` 锁外。 |
| `src/phxbringup/PhxGpuQueue.h` | `phxReadFence64` | 有限次 `hi1 -> lo -> hi2` 重试，消除 64 位 BAR0 Fence 跨 $2^{32}$ 进位撕裂读。 |
| `src/phxbringup/PhxBringup.cpp` | `VmCtx`、`ptRefillReserve`、`ptHashInsert`、`ptTable`、`ptTablePhys`、`vmCtxAllocArrays` | 64 KiB 连续页表块改为 4 KiB 按需散页 + 锁外 16 页小批预分配池（上限 516 页）+ $O(1)$ 开放寻址哈希查表。 |
| `src/phxbringup/PhxBringup.cpp` | `vmBind`、`gpuSubmit`、`vmFlushTlb` 失败回滚 | 等待空闲 VMID/ring 前释放 `gpuLock`，唤醒后重验队列/上下文；前 16 次 `IODelay(50)`，之后 `IOSleep(1)`；TLB 失败回滚 `h.ctx`/`c.hw`。 |
| `src/phxbringup/PhxBringup.cpp` | `isGpuSleeping`、`setPowerState`、`hangCheck`、`reapClients`、`vmidAlloc`/`vmidFree`/`vmMap`/`vmUnmap` | 休眠切换期原子标记与 `gpuLock` 序列化，禁止访问掉电 MMIO。 |
| `src/phxbringup/PhxBringup.cpp` | `PhxBringup::retainAccel`、`accelLock`、`doSurfGpuMark` | 用带锁引用缓存替换静态 `IOService *accel`，`copyMatchingService`/`release()` 全部在锁外完成并在 `stop()` 释放。 |
| `src/phxaccel/accel/phxfb.cpp` | `PhxAMDFB::vblFire`、`displayPower`、`PhxAMDFB::stop` | `fStopping` 原子化，`stop()` 最优先 cancel+free `fVblTimer`，防止与 `poolFree()`/`fMmio` 释放产生竞态。 |

### 3.2 P1 — CPU 降载与 GPU 渲染提速

| 文件 | 关键符号 / 改动 | 预期收益 |
| :--- | :--- | :--- |
| `src/phxbringup/PhxGpuQueue.h`、`src/phxbringup/PhxBringup.cpp` | `phxQueueRetired`、`queueIndexFor`、`gpuRetiredMask`、`lastSeqByQueue`、`seqQueues`、`doSubmit`、`doWaitSeq` | **GFX 与 Compute 队列完成状态解耦**：WindowServer（GFX 队列）等待/唤醒不再被后台重型 Compute 任务拖累；多队列混合客户端 `retireFences` 仍保守取全队列安全序。 |
| `src/phxbringup/PhxBringup.cpp` | `gHasClflushOpt`、CPUID `0x7:0 EBX[23]`、`flushLine`、`vmMap`、`vmUnmap` | 自动启用 `CLFLUSHOPT`；`vmMap` 按物理连续段 `segLen` 与 L0 512 项边界批处理填充 PTE、按 64B cache line 批量刷新并统一 `sfence`；`vmUnmap` 对空 2 MiB L0 子树直接跳过。 |
| `src/phxbringup/PhxBringup.cpp` | `gpuSubmit` | 写 ring 后用 `sfence` 排空 WC buffer，合并写 `wb` 与敲门铃之间的冗余 BAR0 回读及第一次 `hdpFlush()`，每次提交只保留 1 次 `hdpFlush()`。 |
| `src/phxbringup/PhxVaMaps.h`、`src/phxbringup/PhxBringup.cpp` | `PhxVaMap`、`phxVaMapOverlaps`/`InsertAt`/`RemoveAt`/`FindExact`/`FindContaining`/`Covered`、`doGemVa`、`ibCovered`、`doBoFree` | VA 映射表按起始 VA 有序维护，消除 `maps[4096]` $O(N)$ 线性扫描（`doGemVa`/`ibCovered`/`doSubmit`）；`doBoFree` 对**未绑定 GPU VA 的内核 BO** 跳过同步 `waitIdle()` 管线排空气泡（`host` 导入与仍有 GPU 映射的 BO 保留等待）。 |
| `src/nmplugin/nvmtl_vk.h`、`src/nmplugin/nvmtl_vk.c` | `nvk_buffer::bda`、`nvmtl_vk_buffer_address` | 缓存 Buffer Device Address，消除 `nvmtl_vk_bind_buffer` 等热路径重复向 RADV 查询设备地址的开销。 |
| `src/nmplugin/nvmtl_vk.c` | `g_fence_pool`、`nvmtl_fence_acquire`、`nvmtl_fence_release` | 复用已完成的 `VkFence` 池（上限 32），替代每次 `submit_begin`/`submit_finish` 新建与销毁 Vulkan fence。 |
| `src/nmplugin/nvmtl_vk.c` | `nvmtl_pipe_cache_path` / `init` / `save` / `note_compile`、`g_pipe_cache` | 接入持久化磁盘 `VkPipelineCache`（路径 `$TMPDIR/nvmtl-pcache-<uid>-<vendor>-<prog>.bin`），第 8/16/32... 次编译自动原子落盘（并在 `atexit` 保存），显著降低 WindowServer/游戏冷启动及运行期着色器编译 CPU 峰值；全部分支 4 处管线创建调用点均已接入。 |
| `src/nmplugin/nvmtl_vk.c` | `nvmtl_rp_light` | 对 AMD（`0x1002`）下的 `WindowServer` 进程**默认开启** `NVMTL_RP_LIGHT`，省去每个 RenderPass 结束冗余的全管线排空与缓存刷写（可用 `NVMTL_RP_LIGHT=0` 强制回退）。 |
| `src/nmplugin/nvmtl_vk.c` | `nvmtl_vk_cmd_barrier_to_transfer`、`nvmtl_vk_cmd_barrier_from_transfer` | 将 `fill_buffer`/`write_word`/`copy_buffer`/`copy_buffer_to_image`/`copy_image_to_buffer` 前后的全管线屏障收窄为仅覆盖 `TRANSFER` 阶段的屏障。 |

---

## 4. 运行时控制开关与观测指标

- **Boot-args**：
  - `phxfirebat_ec=1`：显式启用 Firebat `0xFE0B0400` EC 风扇曲线控制（UM790 Pro **切勿添加**；未设置时默认完全关闭）。
  - `-phxbringupoff`：紧急禁用 `PhxBringup`（既有安全开关）。
- **环境变量（`NVMTLDriver` 用户态插件）**：
  - `NVMTL_PIPE_CACHE=0`：若怀疑管线磁盘缓存异常，可临时禁用持久化 `VkPipelineCache`（默认开启）。
  - `NVMTL_RP_LIGHT=0`：若出现画面损坏/同步伪影，可关闭 WindowServer AMD 默认轻量 RenderPass 依赖（默认在 AMD WindowServer 开启）。
- **Sysctl 观测 / 调优**：
  - `debug.phxbos_flush`（默认 `0`）、`debug.phx_hang_ms`（默认 `3000`）、`debug.phx_hang_kills`
  - `debug.nvaccel_iop_copy_skip`、`debug.nvaccel_surface_gpu_waits`、`debug.nvaccel_surface_gpu_timeouts`

---

## 5. UM790 Pro macOS 实体机编译与测试步骤

> 请在实体机 macOS 上同步 `feat/um790pro` 后，按仓库内原生脚本编译三大改动组件（注意本分支未改外接显示模块）：

1. **同步分支**：
   ```sh
   git fetch origin
   git checkout feat/um790pro
   git pull --ff-only origin feat/um790pro
   ```
2. **编译并安装核心驱动 / 插件**：
   - `PhxBringup.kext`：在 macOS 运行 `src/phxbringup/build.sh <path-to-MacKernelSDK>` → 生成 `src/phxbringup/build/PhxBringup.kext`
   - `PhxAccel.kext`：在 macOS 运行 `src/phxaccel/build.sh <path-to-MacKernelSDK>` → 生成 `src/phxaccel/build/PhxAccel.kext`
   - `NVMTLDriver` Metal 插件：在 macOS 运行 `src/nmplugin/build.sh [Vulkan-Headers-include-dir]` → 生成 `src/nmplugin/NVMTLDriver.new`
   - 安装与打包按实体机既定部署脚本执行。
3. **实体机重点验证项**：
   - **稳定性与日志**：检查内核启动及运行 15 分钟以上无 `PhxBringup` / `PhxAccel` KP、无 Firebat EC 报错；验证休眠/唤醒 (`pmset sleepnow`) 不出现 MMIO/TLB fault。
   - **CPU 负载对比**：桌面静止 / 窗口拖拽 / 视频播放时观察 `WindowServer` 与 `kernel_task` CPU% 相比上游基线是否下降；检查 `$TMPDIR/nvmtl-pcache-*` 是否正常生成并在重启 `WindowServer` 或二次打开游戏/Metal 应用后命中加快加载。
   - **GPU 渲染与队列解耦**：验证 `glmark2` / Geekbench / Blender / Metal demo 与桌面拖动并发运行时，WindowServer 帧率无明显卡顿；检查 `sysctl debug.nvaccel_iop_copy_skip` 与 `debug.phx_hang_kills` 变化，并确认无花屏、残影或同步撕裂。
   - **回退排查**：若出现花屏请先测 `NVMTL_RP_LIGHT=0`；若出现管线编译异常请先测 `NVMTL_PIPE_CACHE=0` 并清理 `$TMPDIR/nvmtl-pcache-*.bin`。

---

## 6. 实体机 AI 回执模板 (`[HW-Report]`)

> 请实体机测试 AI 在今晚实测完成后，按以下格式汇总发给用户（或追加在本文件末尾第 7 节提交）：

```markdown
### [HW-Report] UM790 Pro 实测回执
- 测试分支与提交：<commit hash>
- 机器系统与内核：<macOS 版本 / UMA 显存大小 / 当前 boot-args>
- 编译安装结果：PhxBringup.kext [成功/失败] | PhxAccel.kext [成功/失败] | NVMTLDriver [成功/失败]
- 基础稳定性：桌面点亮 [通过/失败] | 拖拽流畅度 [表现] | 休眠唤醒 [通过/失败/未测] | Kernel Panic [无/日志摘要]
- 性能与 CPU 对比：
  - WindowServer CPU%（静止 / 拖动 / 负载）：<数值>
  - GPU 基准测试（如 Geekbench Metal / Unigine / 帧率工具）：<对比结果>
  - Compute + 桌面并发掉帧改善情况：<主观/帧率描述>
  - `$TMPDIR/nvmtl-pcache-*` 生成与二次启动加载耗时：<情况>
- Sysctl / 日志关键指标：
  - `debug.phx_hang_kills`: <值>
  - `debug.nvaccel_iop_copy_skip`: <值>
  - `debug.nvaccel_surface_gpu_timeouts`: <值>
- 若有问题/需要下阶段（P2）排查：
  - `NVMTL_RP_LIGHT=0` 测试结果：<正常/异常>
  - `NVMTL_PIPE_CACHE=0` 测试结果：<正常/异常>
  - 关键报错日志片段（脱敏）：
```

---

## 7. 实测反馈追加区（实体机 AI 填写）

_待今晚 UM790 Pro 实机测试后追加。_
