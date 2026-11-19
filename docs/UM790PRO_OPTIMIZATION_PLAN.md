# 铭凡 UM790 Pro 适配与优化路线图 (`feat/um790pro`)

## 1. 分支目标

本分支基于上游 `luuumi-dev/nullarc`（`main @ 3497d87`），面向 **铭凡（Minisforum）UM790 Pro**（AMD Ryzen 9 7940HS / Radeon 780M，RDNA 3 `gfx1103`，macOS Tahoe 26），聚焦以下目标：

1. **硬件安全基线**：消除针对特定笔记本机型的 EC 硬编码盲写与内核内存泄漏，确保在 UM790 Pro 迷你主机上的长期稳定运行。
2. **降低 CPU 负载**：消除内核态忙等持锁、冗余 MMIO/BAR0 回读、高频逐页页表刷写与渲染热路径 CPU 重复开销。
3. **提升 GPU 渲染效率**：解耦 GFX 与 Compute 队列、消除同步排空管线的气泡、完善 Vulkan/Metal 管线与屏障优化。
4. **提高系统稳定性**：修复并发 Use-After-Free、64 位 Fence 撕裂读、休眠/卸载生命周期缺陷与 Wired 内存失控问题。

---

## 2. 合并边界约束（重要）

> **并行开发约定**：外接 HDMI / USB4 Type-C DP 显示输出适配正在另一条分支并行推进，后续将合入本分支的优化工作。为最大程度避免合并冲突，**本分支严格禁止修改外接显示相关代码**：
>
> - `src/amd/phx_dcn*`
> - `src/amd/phx_dmub*`
> - `src/amd/phx_hdmi*`
> - `src/phxaccel/accel/phxfb.cpp` 中的 `PhxHDMIHead` / `PhxHDMINub` / `phxSpawnHdmi` 相关逻辑

---

## 3. P0 — UM790 Pro 安全基线与内核稳定性修复

### 3.1 `src/phxaccel/accel/nvrm-accel.cpp`：Firebat EC 盲写隔离与 `ml_io_map` VA 泄漏修复
- **门控隔离**：将 `0xFE0B0400`（Firebat U6C 专属 EC MMIO）写入与 `fanWorker` 置于严格的主板/ACPI/SMBIOS 校验及显式 boot-arg（如 `phxfan=1`）之后；在 UM790 Pro 等非 Firebat 硬件上默认完全禁用，避免盲写未知 MMIO 地址导致随机重启或供电异常。
- **消除泄漏**：启用时仅在驱动初始化阶段执行一次 `ml_io_map` 并持久化映射对象，在 `stop()` 中统一释放，彻底解决 `fanWorker` 每秒一次（86,400 次/天）及 `firebat_fan_mode_sysctl` 的内核 VA 泄漏。

### 3.2 `src/phxbringup/PhxBringup.cpp`：核心并发、锁粒度与内存/时钟安全
- **修复 `doSurfGpuMark` 并发 UAF**：将静态局部 `IOService *accel` 迁至受锁/原子引用保护的成员变量，每次调用前 `retain()`、调用后 `release()`，消除 `isInactive()` 检查与 `callPlatformFunction()` 之间的释放后使用竞态。
- **避免持 `gpuLock` 忙等**：重构 `vmBind()` 与 `gpuSubmit()` 中的退避等待逻辑，在 `IODelay`/`IOSleep` 前先释放 `gpuLock` 并在唤醒后重新校验状态，防止单客户端等待（最长 ~2 秒）锁死全系统所有 GPU 提交与 `hangCheck()`。
- **修复 `queueFence` 64 位撕裂读**：将 BAR0 两次独立的 32 位读改为 `hi1 -> lo -> hi2` 重试循环（或配合 snooped 系统内存的 64 位原子读），避免低 32 位进位瞬间读出跳变 $2^{32}$ 的错误序号。
- **消除持全局 `gpuLock` 申请 64 KiB 物理连续页表**：改为按需分配单个 4 KiB 非连续物理页（锁外预分配），避免长时间运行后因内存碎片导致页表分配失败或阻塞全局 GPU 锁。
- **完善 `isSleeping` 状态守卫**：在 `gpuSubmit()`、`hangCheck()` 及寄存器访问路径检查 `isSleeping` 状态，防止休眠切换期访问已掉电 MMIO。

### 3.3 `src/phxaccel/accel/nvrm-accel.cpp` & `src/phxaccel/accel/phxfb.cpp`：显示同步与卸载秩序
- **完善 `nvSurfMark` / `gSurfPend` 并发语义**：为待处理表面标记引入引用计数或单调代数，防止并发重叠帧提前清标；对 `nvSurfWaitReady()` 超时场景做安全处理（跳过本次读回或保留上一帧），避免读取未写完的表面导致屏幕残影（B-58）。
- **加固 `gIopFlipCache` 生命周期**：保留对 `NVResource` / `NVVidMemory` 的安全引用或在资源析构时加锁失效缓存项，消除无 family lock 下的悬空指针解引用。
- **修正 `PhxAMDFB::stop` 卸载顺序**：先取消并释放 `fVblTimer` 确保无在途定时器回调，再释放 `fAliasMap`、VRAM 池（`poolFree()`）和 MMIO 映射；如有需要，严格保护拷贝路径中的浮点/SIMD 状态，避免破坏用户态 AVX/YMM 上下文。

---

## 4. P1 — 降低 CPU 负载与提升 GPU 渲染效率

### 4.1 `src/phxbringup/PhxBringup.cpp`：队列解耦、延迟回收与页表批处理
- **分队列追踪 Fence 退休（GFX vs Compute 解耦）**：将 `gpuRetiredLocked()` 从跨 `gq[0]`/`gq[1]` 取最小完成序号改为按队列独立追踪（或返回 per-queue retired 向量），避免后台重度 Compute 任务拖垮 WindowServer 在 GFX 队列上的帧完成判定与唤醒。
- **客户端延迟 BO 解映射/释放队列**：消除 `doBoFree` 与 `doGemVa(UNMAP)` 中同步 `waitIdle()` 造成的 CPU-GPU 管线排空气泡；改为记录安全退休序号 `lastSeq` 并立即返回，待 GPU 越过该序号后由提交路径或 reaper 批量解映射、合并单次 TLB flush 并释放。
- **`vmMap` 按物理连续段批处理**：利用 `getPhysicalSegment()` 返回的 `segLen` 连续映射到同一张 L0 页表，按 64 字节 cache line 批量执行 `clflushopt` 并仅在结尾做一次 `sfence`，消除逐页查表和每 8 字节一次 `clflush` 的 8 倍冗余刷新。
- **替换 `maps[4096]` 线性扫描**：将 `ibCovered()` 与 `doGemVa` 重叠检查改为按 VA 有序数组二分查找或区间索引，消除 O(N) 全表扫描开销。
- **合并 `gpuSubmit` 冗余硬件刷写**：梳理并合并写 ring 与写 `wb` 之间的冗余 `hdpFlush()`（BAR0 MMIO 回读）与重复内存屏障，降低每次提交的 CPU 与 PCIe 往返成本。

### 4.2 `src/nmplugin/nvmtl_vk.c`（及上层绑定）：Vulkan 管线、同步与带宽优化
- **启用持久化 `VkPipelineCache`**：为图形/计算管线及 compute threadgroup 变体接入共享 `VkPipelineCache` 并持久化到缓存目录，降低应用冷启动与着色器编译期 CPU 峰值。
- **缓存 Buffer Device Address（BDA）**：在 `nvk_buffer` 上缓存 `vkGetBufferDeviceAddress` 结果，消除 `nvmtl_vk_bind_buffer` 每次绑定重复调用驱动查询接口的开销。
- **池化 `VkFence` 与优化 RenderPass/Framebuffer 开销**：复用 `VkFence` 对象池替代每次提交新建/销毁；在轻量路径推广 `NVMTL_RP_LIGHT` 默认化，透传 Metal `MTLLoadActionDontCare` / `MTLStoreActionDontCare`（尤其 MSAA 与临时深度缓冲）以节省 780M 共享内存带宽；探索复用 `VkFramebuffer` 或迁移至 `VK_KHR_dynamic_rendering`。
- **精简 Blit/Copy 过度串行屏障与推进异步上传**：合并 Blit Encoder 内连续无冲突拷贝的全管线屏障（`ALL_COMMANDS -> ALL_COMMANDS`），推进 `NVMTL_ASYNC_TEX_UPLOAD` 稳态化，减少同步等 fence 对 OpenGL/3D 帧率的阻塞。
- **缓解 Wired 内存压力（B-32）**：按需缩减 `PhxAMDFB` 非必要预留连续池规模，探索 `PhxBringup` 全局跨客户端 wired 内存预算上限与低内存回收联动。

---

## 5. 验证策略

- **宿主机单元测试（每项改动必跑）**：
  - `make test`（所有 C++ 单元测试与 Python 测试）
  - `python3 tests/test_phx_profile.py`
  - 为新增的纯逻辑 helper（如安全队列查询、延迟释放队列、区间二分查找、64 位序号读取、EC 门控策略等）补充对应 host C++ 单元测试。
- **真机实测（UM790 Pro / macOS Tahoe 26）**：
  - 验证无 Firebat EC 报错或异常重启，内核日志无持续 `ml_io_map` 泄漏或 UAF panic。
  - 对比优化前后的 WindowServer CPU 占用、Metal 3D/Compute 基准帧率与延迟、以及并跑 Compute + 桌面合成时的掉帧表现。
