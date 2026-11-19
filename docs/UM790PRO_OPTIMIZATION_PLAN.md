# 铭凡 UM790 Pro 适配与优化路线图 (`feat/um790pro`)

## 1. 分支目标

本分支基于上游 `luuumi-dev/nullarc`（`main @ 3497d87`），面向 **铭凡（Minisforum）UM790 Pro**（AMD Ryzen 9 7940HS / Radeon 780M，RDNA 3 `gfx1103`，macOS Tahoe 26），聚焦以下目标：

1. **硬件安全基线**：消除针对特定笔记本机型的 EC 硬编码盲写与内核内存泄漏，确保在 UM790 Pro 迷你主机上的长期稳定运行。
2. **降低 CPU 负载**：消除内核态忙等持锁、冗余 MMIO/BAR0 回读、高频逐页页表刷写与渲染热路径 CPU 重复开销。
3. **提升 GPU 渲染效率**：解耦 GFX 与 Compute 队列、消除同步排空管线的气泡、完善 Vulkan/Metal 管线与屏障优化。
4. **提高系统稳定性**：修复并发 Use-After-Free、64 位 Fence 撕裂读、休眠/卸载生命周期缺陷与 Wired 内存失控问题。

> **双端 AI 协同与实机测试记录**：请参见 [`docs/AI_SYNC_LOG.md`](AI_SYNC_LOG.md)（含 P0/P1 已合入提交号、控制开关、实体机编译清单及 `[HW-Report]` 反馈模板）。

---

## 2. 合并边界约束（重要）

> **并行开发约定**：外接 HDMI / USB4 Type-C DP 显示输出适配正在另一条分支并行推进，后续将合入本分支的优化工作。为最大程度避免合并冲突，**本分支严格禁止修改外接显示相关代码**：
>
> - `src/amd/phx_dcn*`
> - `src/amd/phx_dmub*`
> - `src/amd/phx_hdmi*`
> - `src/phxaccel/accel/phxfb.cpp` 中的 `PhxHDMIHead` / `PhxHDMINub` / `phxSpawnHdmi` 相关逻辑

---

## 3. P0 — UM790 Pro 安全基线与内核稳定性修复（✅ 已完成并经 CI 验证）

### 3.1 `src/phxaccel/accel/nvrm-accel.cpp`：Firebat EC 盲写隔离与 `ml_io_map` VA 泄漏修复
- [x] **门控隔离**：将 `0xFE0B0400`（Firebat U6C 专属 EC MMIO）写入与 `fanWorker` 置于显式 boot-arg `phxfirebat_ec=1` 之后；UM790 Pro 默认完全禁用。
- [x] **消除泄漏**：完全移除 `ml_io_map`，仅在 `phxfirebat_ec=1` 时通过 `IODeviceMemory::withRange` 映射一次并在 `stop()` 统一释放。

### 3.2 `src/phxbringup/PhxBringup.cpp`：核心并发、锁粒度与内存/时钟安全
- [x] **修复 `doSurfGpuMark` 并发 UAF**：引入 `retainAccel()` / `accelLock` 管理 `cachedAccel` 与 `surfVramSym`，在锁外完成 `copyMatchingService`/`release()` 并在 `stop()` 清理。
- [x] **避免持 `gpuLock` 忙等**：重构 `vmBind()` 与 `gpuSubmit()` 等待逻辑，等待前释放 `gpuLock`、前 16 次 `IODelay(50)` 之后 `IOSleep(1)` 并在重拿锁后全量重校验状态。
- [x] **修复 `queueFence` 64 位撕裂读**：新增 `phxReadFence64()`（`hi1 -> lo -> hi2` 重试循环）。
- [x] **消除持全局 `gpuLock` 申请 64 KiB 物理连续页表**：改为按需分配单个 4 KiB 页面、锁外 16 页小批填充预留池（`ptReserve`，上限 516 页）与 $O(1)$ 开放寻址哈希查表。
- [x] **完善 `isSleeping` 状态守卫**：`setPowerState` 与 `gpuSubmit`/`vmBind`/`vmMap`/`vmUnmap`/`vmidAlloc`/`vmidFree`/`hangCheck`/`reapClients` 全链路守卫。

### 3.3 `src/phxaccel/accel/nvrm-accel.cpp` & `src/phxaccel/accel/phxfb.cpp`：显示同步与卸载秩序
- [x] **完善 `nvSurfMark` / `gSurfPend` 并发语义**：新增 `refs` 引用计数；`performTransaction` 在 `nvSurfWaitReady()` 超时时跳过拷屏（`gCntIopCopySkip`）保上一帧画面。
- [x] **加固 `gIopFlipCache` 生命周期**：缓存引用计数化、`IOSimpleLock` 锁外释放、`NVScopedRetain` 持用保护，以及 `NVResource::free`/`NVVidMemory::deallocPhysical`/`free` 清理勾子。
- [x] **修正 `PhxAMDFB::stop` 卸载顺序**：`fStopping` 原子化，`stop()` 优先取消并释放 `fVblTimer` 后再释放映射与 `poolFree()`（未改动任何 HDMI 类）。

---

## 4. P1 — 降低 CPU 负载与提升 GPU 渲染效率（核心项已实现，待实体机评测 & 后续深挖）

### 4.1 `src/phxbringup/PhxBringup.cpp`：队列解耦、延迟回收与页表批处理
- [x] **分队列追踪 Fence 退休（GFX vs Compute 解耦）**：新增 `phxQueueRetired` 与 `gpuRetiredMask`、`lastSeqByQueue`/`seqQueues`，解耦 `doWaitSeq` 对 Compute/GFX 的跨队列相互拖累。
- [x] **免无效等待快速路径（`doBoFree`）**：对无 GPU VA 映射的内核 BO 在 `doBoFree` 跳过同步 `waitIdle()` 管线排空气泡；`host` 导入与已映射 BO 仍安全等待。（注：全异步延后队列回收留作后续按实测需要演进。）
- [x] **`vmMap` / `vmUnmap` 按物理连续段批处理 + `CLFLUSHOPT`**：CPUID 检测 `CLFLUSHOPT`；`vmMap`/`vmUnmap` 按 `segLen` 与 L0 512 项边界批填 PTE、按 64B cache line 刷写并在结尾统一 `sfence`；`vmUnmap` 跳过空 L0 子树。
- [x] **替换 `maps[4096]` 线性扫描**：抽出 `PhxVaMaps.h`，VA 区间按起始地址有序维护并在 `doGemVa` / `ibCovered` / `doSubmit` 改用 $O(\log N)$ 二分查找。
- [x] **合并 `gpuSubmit` 冗余硬件刷写**：写 ring 后以 `sfence` 隔离 `wb`，合并为单次 `hdpFlush()` + `mfence` 敲门铃。

### 4.2 `src/nmplugin/nvmtl_vk.c`（及上层绑定）：Vulkan 管线、同步与带宽优化
- [x] **启用持久化 `VkPipelineCache`**：`$TMPDIR/nvmtl-pcache-<uid>-<vendor>-<prog>.bin` 磁盘持久化（加载 + `atexit` 与第 8/16/32... 次编译自动落盘），接入 4 处图形/计算管线创建路径。
- [x] **缓存 Buffer Device Address（BDA）**：`nvk_buffer::bda` 原子缓存 `nvmtl_vk_buffer_address()` 结果。
- [x] **池化 `VkFence` 与默认 `NVMTL_RP_LIGHT`**：复用至多 32 个 `VkFence` 替代高频提交新建/销毁；针对 AMD（`0x1002`）下的 `WindowServer` 默认开启 `NVMTL_RP_LIGHT` 消除 Pass 出口冗余全管线排空。
- [x] **精简 Blit/Transfer 过度串行屏障**：`fill_buffer`/`write_word`/`copy_buffer`/`copy_buffer_to_image`/`copy_image_to_buffer` 改用 `ALL_COMMANDS <-> TRANSFER` 窄化阶段屏障。
- [ ] **后续 P1.5 / P2 备选（视今晚实体机基准决定）**：`VK_KHR_dynamic_rendering` / `VkFramebuffer` 复用、`NVMTL_ASYNC_TEX_UPLOAD` 稳态化、Blit Encoder 连续无冲突拷入合并、全局 Wired 内存上限与低内存回收（B-32）。

---

## 5. 验证策略

- **宿主机单元测试（每项改动必跑）**：
  - `make test`（所有 C++ 单元测试与 Python 测试）
  - `python3 tests/test_phx_profile.py`
  - 为新增的纯逻辑 helper（如安全队列查询、延迟释放队列、区间二分查找、64 位序号读取、EC 门控策略等）补充对应 host C++ 单元测试。
- **真机实测（UM790 Pro / macOS Tahoe 26）**：
  - 验证无 Firebat EC 报错或异常重启，内核日志无持续 `ml_io_map` 泄漏或 UAF panic。
  - 对比优化前后的 WindowServer CPU 占用、Metal 3D/Compute 基准帧率与延迟、以及并跑 Compute + 桌面合成时的掉帧表现。
