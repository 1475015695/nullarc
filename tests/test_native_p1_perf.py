"""Host unit tests for P1 CPU load reduction and GPU pipeline optimizations."""
import pathlib
import subprocess
import tempfile
import unittest

from test_native_bo_lifetime import method

ROOT = pathlib.Path(__file__).resolve().parents[1]


class NativeP1PerfTest(unittest.TestCase):
    def test_bringup_p1_batch_vm_single_hdp_decoupled_wait_and_bo_free(self):
        source = (ROOT / 'src/phxbringup/PhxBringup.cpp').read_text()
        code = r'''
#include <cassert>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include "phxbringup/PhxNativeABI.h"
#include "phxbringup/PhxGpuQueue.h"
#include "phxbringup/PhxPendingFences.h"
#include "phxbringup/PhxRange.h"
#include "phxbringup/PhxVaMaps.h"

using IOReturn = int;
using IOByteCount = uint64_t;
using addr64_t = uint64_t;
constexpr int kIOReturnSuccess = 0, kIOReturnBadArgument = 1, kIOReturnNotReady = 2;
constexpr int kIOReturnVMError = 3, kIOReturnNoMemory = 4, kIOReturnNotFound = 5, kIOReturnTimeout = 6;
constexpr int kIOReturnUnsupported = 7, kIOReturnNoResources = 8, kIOReturnBusy = 9, kIOReturnNotOpen = 10;
constexpr int kIOMemoryMapperNone = 0, kMaxMaps = 64;
constexpr uint64_t kPtAddrMask = 0x0000FFFFFFFFF000ull;
constexpr uint64_t kPteSysFlags = 0x77ull | (4ull << 48);
constexpr uint64_t kVaMask = (1ull << 48) - 1;
constexpr uint32_t kFirstUserVmid = 8, kMaxCtx = 16;

struct Lock { bool held = false; };
static void IOLockLock(Lock *l) { assert(!l->held); l->held = true; }
static void IOLockUnlock(Lock *l) { assert(l->held); l->held = false; }
void bzero(void *p, size_t n) { std::memset(p, 0, n); }
void bcopy(const void *s, void *d, size_t n) { std::memmove(d, s, n); }
template<class... T> static void IOLog(const char *, T...) {}

static unsigned gFlushLineCalls = 0;
static void flushLine(const volatile void *) { gFlushLineCalls++; }
static void IODelay(unsigned) {}
static void IOSleep(unsigned) {}
static void *IOMallocZero(size_t n) { return std::calloc(1, n); }
static void IOFree(void *p, size_t) { std::free(p); }

class IOMemoryDescriptor {
public:
    uint64_t len{65536};
    uint64_t segLen{4096};
    unsigned segCalls{0};
    unsigned refs{1};
    uint64_t getLength() const { return len; }
    addr64_t getPhysicalSegment(uint64_t off, IOByteCount *outLen, int) {
        segCalls++;
        *outLen = segLen;
        return 0x20000000ull + off;
    }
    void retain() { refs++; }
    void release() { assert(refs); refs--; }
};

static bool vaCanonical(uint64_t va) {
    uint64_t top = va >> 47;
    return top == 0 || top == 0x1FFFF;
}

struct PhxBringup {
    static constexpr uint32_t kPtReserveCap = 516;
    struct VmCtx {
        bool busy = true;
        uint32_t hw = 8;
        alignas(64) uint64_t l0[4][512]{};
        bool l0Allocated[4]{true, false, false, false};
    } vm[kMaxCtx];
    struct HwVmid { uint32_t ctx = 1; uint64_t lastSeq = 0, lastUse = 0, binding = 1; } hwv[8];
    using GpuQueue = PhxGpuQueue;
    GpuQueue gq[2]{};
    struct Aper {
        alignas(64) uint8_t mem[8192]{};
        uintptr_t getVirtualAddress() { return reinterpret_cast<uintptr_t>(mem); }
    } aper, db;
    Aper *fbAper = &aper, *doorbellMap = &db;
    Lock mutex;
    Lock *gpuLock = &mutex;
    uint64_t gSeq = 0, fbMcBase = 0x8000000000ull;
    bool stopped = false, sleeping = false, fault = false, ready = true;
    unsigned refills = 0, pteLookups = 0, tlbFlushes = 0, hdpFlushes = 0, packetWrites = 0;

    bool isStopped() const { return stopped; }
    bool isGpuSleeping() const { return sleeping; }
    bool vmHealthy() const { return !fault; }
    bool gpuReady() const { return ready; }
    bool debugEnabled() const { return false; }
    uint8_t queueIndexFor(bool gfx) const { return gfx ? (gq[1].ready ? 1u : 0u) : (gq[0].ready ? 0u : 1u); }
    void ptRefillReserve(uint32_t) { refills++; }
    VmCtx *vmCtx(uint32_t h) { return h && h <= kMaxCtx && vm[h - 1].busy ? &vm[h - 1] : nullptr; }
    bool vmFlushTlb(uint32_t) { assert(gpuLock->held); tlbFlushes++; return true; }
    uint32_t vmBind(uint32_t handle) { return vm[handle - 1].hw ? vm[handle - 1].hw : 8; }
    void ringPut(GpuQueue &, uint32_t, uint32_t) { packetWrites++; }
    void hdpFlush() { hdpFlushes++; }
    uint64_t queueFence(const GpuQueue &q) const {
        const volatile uint32_t *f = reinterpret_cast<const volatile uint32_t *>(aper.mem + q.fenceOff);
        return phxReadFence64(f);
    }
    uint64_t *vmPte(VmCtx &c, uint64_t va, bool alloc) {
        assert(gpuLock->held);
        pteLookups++;
        uint32_t tbl = static_cast<uint32_t>(va >> 21);
        assert(tbl < 4);
        if (!c.l0Allocated[tbl]) {
            if (!alloc) return nullptr;
            c.l0Allocated[tbl] = true;
        }
        return &c.l0[tbl][(va >> 12) & 511];
    }
    uint64_t gpuRetired() const { return gpuRetiredMask(0x3); }
    uint64_t gpuRetiredMask(uint8_t queueMask) const;
    uint64_t gpuRetiredLockedMask(uint8_t queueMask) const;
    IOReturn vmMap(uint32_t handle, uint64_t va, IOMemoryDescriptor *md, uint64_t off, uint64_t size);
    IOReturn vmUnmap(uint32_t handle, uint64_t va, uint64_t size);
    IOReturn gpuSubmit(uint32_t handle, bool gfx, const uint64_t *ibVa, const uint32_t *ibDw, uint32_t n, uint64_t *seqOut);
};

struct IOExternalMethodArguments {
    uint32_t scalarInputCount{0}, scalarOutputCount{0};
    uint32_t structureInputSize{0}, structureOutputSize{0};
    void *structureInputDescriptor{nullptr}, *structureOutputDescriptor{nullptr};
    const void *structureInput{nullptr};
    uint64_t scalarInput[4]{}, scalarOutput[4]{};
};
static bool shape(const IOExternalMethodArguments *a, uint32_t si, uint32_t so, uint32_t sti, uint32_t sto) {
    return a->scalarInputCount == si && a->scalarOutputCount == so &&
           a->structureInputSize == sti && a->structureOutputSize == sto;
}

struct IOAccelPhxNativeClient {
    struct Bo { IOMemoryDescriptor *md = nullptr; uint64_t size = 65536; uint32_t domain = 2, bits = 0; uint8_t *kva = nullptr; bool host = false; } bo[4];
    using Map = PhxVaMap;
    Map maps[kMaxMaps]{};
    int mapCount = 0;
    PhxBringup *owner = nullptr;
    Lock clientMutex;
    Lock *lock = &clientMutex;
    bool hello = true, closing = false;
    bool ctx[PHXN_MAX_CTX + 1]{};
    uint32_t vmid = 1;
    uint64_t emitted = 0, lastSeq = 0;
    uint64_t visUsed = 0, hiUsed = 0, gttUsed = 65536, hostUsed = 0;
    uint64_t lastSeqByQueue[2]{0, 0};
    struct SeqQueue { uint64_t seq; uint8_t qi; } seqQueues[64]{};
    uint32_t seqQueueHead = 0;
    uint64_t lastRetiredFencesArg = ~0ull;
    unsigned retireCalls = 0, waitIdleCalls = 0, flushCalls = 0;
    PhxPendingFences pendingFences;

    IOAccelPhxNativeClient() { ctx[1] = true; }
    uint8_t queueMaskForTarget(uint64_t target) const {
        for (uint32_t i = 0; i < 64; i++) {
            if (seqQueues[i].seq == target)
                return static_cast<uint8_t>(1u << seqQueues[i].qi);
        }
        uint8_t mask = 0;
        if (lastSeqByQueue[0]) mask |= 1u;
        if (lastSeqByQueue[1]) mask |= 2u;
        return mask ? mask : 0x3u;
    }
    Bo *findBo(uint32_t h) { return h && h < 4 && bo[h].md ? &bo[h] : nullptr; }
    void boRelease(Bo &b) { b.md = nullptr; b = Bo{}; }
    bool ensureVmid() { return true; }
    void flushBos() { flushCalls++; }
    void retireFences(uint64_t r, bool = true) { retireCalls++; lastRetiredFencesArg = r; }
    bool waitIdle() {
        waitIdleCalls++;
        return owner->gpuRetired() >= lastSeq;
    }
    IOReturn doBoFree(IOExternalMethodArguments *a);
    IOReturn doGemVa(IOExternalMethodArguments *a);
    bool ibCovered(uint64_t vaCanon, uint64_t bytes);
    IOReturn doSubmit(IOExternalMethodArguments *a);
    IOReturn doWaitSeq(IOExternalMethodArguments *a);
};
'''
        for s in (
            'IOReturn PhxBringup::vmMap(',
            'IOReturn PhxBringup::vmUnmap(',
            'uint64_t PhxBringup::gpuRetiredMask(',
            'uint64_t PhxBringup::gpuRetiredLockedMask(',
            'IOReturn PhxBringup::gpuSubmit(',
            'IOReturn IOAccelPhxNativeClient::doBoFree(',
            'IOReturn IOAccelPhxNativeClient::doGemVa(',
            'bool IOAccelPhxNativeClient::ibCovered(',
            'IOReturn IOAccelPhxNativeClient::doSubmit(',
            'IOReturn IOAccelPhxNativeClient::doWaitSeq(',
        ):
            code += method(source, s)

        code += r'''
static void setFence(PhxBringup &br, uint32_t off, uint64_t val) {
    br.aper.mem[off + 0] = static_cast<uint8_t>(val & 0xff);
    br.aper.mem[off + 1] = static_cast<uint8_t>((val >> 8) & 0xff);
    br.aper.mem[off + 2] = static_cast<uint8_t>((val >> 16) & 0xff);
    br.aper.mem[off + 3] = static_cast<uint8_t>((val >> 24) & 0xff);
    br.aper.mem[off + 4] = static_cast<uint8_t>((val >> 32) & 0xff);
    br.aper.mem[off + 5] = static_cast<uint8_t>((val >> 40) & 0xff);
    br.aper.mem[off + 6] = static_cast<uint8_t>((val >> 48) & 0xff);
    br.aper.mem[off + 7] = static_cast<uint8_t>((val >> 56) & 0xff);
}

int main() {
    // 1. Batched vmMap across 64 KiB contiguous segment -> 1 seg lookup, 1 vmPte lookup, 2 cache-line flushes.
    PhxBringup owner;
    IOMemoryDescriptor md;
    md.len = 65536;
    md.segLen = 65536;
    gFlushLineCalls = 0;
    assert(owner.vmMap(1, 0x100000, &md, 0, 65536) == kIOReturnSuccess);
    assert(md.segCalls == 1);
    assert(owner.pteLookups == 1);
    assert(gFlushLineCalls == 2); // 16 * 8B = 128B = 2 cache lines
    for (int i = 0; i < 16; i++)
        assert((owner.vm[0].l0[0][256 + i] & kPtAddrMask) == 0x20000000ull + static_cast<uint64_t>(i) * 4096);

    // Map spanning 2 MiB L0 boundary [0x1ff000, 0x203000) (4 pages: 1 in l0[0][511], 3 in l0[1][0..2]).
    md.segCalls = 0; owner.pteLookups = 0;
    assert(owner.vmMap(1, 0x1ff000, &md, 0, 16384) == kIOReturnSuccess);
    assert(md.segCalls == 1 && owner.pteLookups == 2);
    assert((owner.vm[0].l0[0][511] & kPtAddrMask) == 0x20000000ull);
    assert((owner.vm[0].l0[1][0] & kPtAddrMask) == 0x20001000ull);
    assert((owner.vm[0].l0[1][2] & kPtAddrMask) == 0x20003000ull);

    // Sparse vmUnmap over 4 MiB skips unallocated L0 table 2 in 1 lookup instead of per-page scanning.
    owner.pteLookups = 0;
    assert(owner.vmUnmap(1, 0x100000, 1ull << 22) == kIOReturnSuccess);
    assert(owner.pteLookups == 3); // L0 tables 0, 1, 2
    assert(!owner.vm[0].l0[0][256] && !owner.vm[0].l0[1][0]);

    // 2. Sorted O(log N) GemVa + ibCovered across split adjacent maps + single HDP flush in gpuSubmit.
    owner.gq[0].ready = true; owner.gq[0].ringDw = 1024; owner.gq[0].fenceOff = 0; owner.gq[0].wbOff = 64;
    owner.gq[1].ready = true; owner.gq[1].ringDw = 1024; owner.gq[1].fenceOff = 16; owner.gq[1].wbOff = 128; owner.gq[1].gfx = true;

    IOAccelPhxNativeClient computeClient; computeClient.owner = &owner; computeClient.bo[1].md = &md;
    IOAccelPhxNativeClient gfxClient; gfxClient.owner = &owner; gfxClient.bo[1].md = &md; gfxClient.bo[2].md = &md;

    // Insert higher VA first, lower adjacent VA second -> kept sorted and ibCovered crosses boundary cleanly.
    phxn_gem_va g{};
    g.handle = 1; g.operation = PHX_AMDGPU_VA_OP_MAP; g.va_address = 0x20000; g.map_size = 4096;
    g.flags = PHX_AMDGPU_VM_PAGE_EXECUTABLE;
    IOExternalMethodArguments ga{}; ga.structureInput = &g; ga.structureInputSize = sizeof(g);
    assert(gfxClient.doGemVa(&ga) == kIOReturnSuccess);
    g.va_address = 0x10000; g.map_size = 0x10000;
    assert(gfxClient.doGemVa(&ga) == kIOReturnSuccess);
    assert(gfxClient.mapCount == 2 && gfxClient.maps[0].va == 0x10000 && gfxClient.maps[1].va == 0x20000);
    assert(gfxClient.ibCovered(0x1f000, 8192));
    g.va_address = 0x18000; g.map_size = 4096;
    assert(gfxClient.doGemVa(&ga) == kIOReturnBadArgument); // overlap rejected

    // Compute submit seq 1 on gq[0]; assert single hdpFlush per submit.
    g.handle = 1; g.va_address = 0x40000; g.map_size = 4096; g.flags = PHX_AMDGPU_VM_PAGE_EXECUTABLE;
    assert(computeClient.doGemVa(&ga) == kIOReturnSuccess);
    struct Packet { phxn_cs_in h; phxn_cs_ib ib; } pkt{};
    pkt.h.abi = PHXN_ABI_VERSION; pkt.h.num_ibs = 1; pkt.h.ctx_id = 1;
    pkt.ib.ip_type = PHX_AMDGPU_HW_IP_COMPUTE; pkt.ib.va_start = 0x40000; pkt.ib.ib_bytes = 4;
    IOExternalMethodArguments sa{}; sa.scalarOutputCount = 1; sa.structureInput = &pkt; sa.structureInputSize = sizeof(pkt);
    owner.hdpFlushes = 0;
    assert(computeClient.doSubmit(&sa) == kIOReturnSuccess && sa.scalarOutput[0] == 1);
    assert(owner.hdpFlushes == 1 && computeClient.lastSeqByQueue[0] == 1 && computeClient.lastSeqByQueue[1] == 0);

    // GFX submit seq 2 on gq[1].
    pkt.ib.ip_type = PHX_AMDGPU_HW_IP_GFX; pkt.ib.va_start = 0x1f800; pkt.ib.ib_bytes = 8;
    assert(gfxClient.doSubmit(&sa) == kIOReturnSuccess && sa.scalarOutput[0] == 2);
    assert(owner.hdpFlushes == 2 && gfxClient.lastSeqByQueue[1] == 2);

    // Advance ONLY GFX fence to 2 (Compute fence stays 0 = stuck on seq 1).
    // GFX-only client must retire immediately without stalling on stalled Compute queue.
    setFence(owner, 16, 2);
    IOExternalMethodArguments wa{};
    wa.scalarInputCount = 3; wa.scalarOutputCount = 3;
    wa.scalarInput[0] = PHXN_SEQ_LAST; wa.scalarInput[1] = 1000000; wa.scalarInput[2] = 0;
    assert(gfxClient.doWaitSeq(&wa) == kIOReturnSuccess);
    assert(wa.scalarOutput[0] == 0 && wa.scalarOutput[1] >= 2 && gfxClient.lastRetiredFencesArg == wa.scalarOutput[1]);

    // Hybrid client submits seq 3 on GFX after seq 1 on Compute; GFX retires while Compute stays 0.
    // Waiting on GFX seq 3 succeeds immediately, while retireFences receives safe cross-queue seq (0 < 1).
    pkt.ib.ip_type = PHX_AMDGPU_HW_IP_COMPUTE; pkt.ib.va_start = 0x40000; pkt.ib.ib_bytes = 4;
    IOAccelPhxNativeClient hybrid; hybrid.owner = &owner; hybrid.bo[1].md = &md;
    hybrid.maps[0] = PhxVaMap{true, 1, 0x10000, 0x50000, 0, PHX_AMDGPU_VM_PAGE_EXECUTABLE};
    hybrid.mapCount = 1;
    assert(hybrid.doSubmit(&sa) == kIOReturnSuccess && sa.scalarOutput[0] == 3); // compute seq 3
    pkt.ib.ip_type = PHX_AMDGPU_HW_IP_GFX;
    assert(hybrid.doSubmit(&sa) == kIOReturnSuccess && sa.scalarOutput[0] == 4); // gfx seq 4
    setFence(owner, 16, 4); // gfx done through 4, compute still stuck on 1
    assert(hybrid.doWaitSeq(&wa) == kIOReturnSuccess && wa.scalarOutput[0] == 0 && wa.scalarOutput[1] >= 4);
    assert(hybrid.lastRetiredFencesArg < 3); // did NOT falsely retire pending compute seq 3!

    // 3. doBoFree skips waitIdle on unmapped kernel BO (even when GPU hasn't retired lastSeq),
    //    but waits on host-imported BOs.
    hybrid.bo[2].md = &md; hybrid.bo[2].host = false; hybrid.gttUsed = 65536;
    IOExternalMethodArguments bf{}; bf.scalarInputCount = 1; bf.scalarInput[0] = 2;
    hybrid.waitIdleCalls = 0;
    assert(hybrid.doBoFree(&bf) == kIOReturnSuccess && hybrid.waitIdleCalls == 0);
    hybrid.bo[3].md = &md; hybrid.bo[3].host = true; hybrid.hostUsed = 65536;
    bf.scalarInput[0] = 3;
    assert(hybrid.doBoFree(&bf) == kIOReturnBusy && hybrid.waitIdleCalls == 1);
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp)
            (path / 'test_p1_bringup.cpp').write_text(code)
            subprocess.run(['clang++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                            '-fsanitize=address,undefined', '-I' + str(ROOT / 'src'),
                            str(path / 'test_p1_bringup.cpp'), '-o', str(path / 'test_p1_bringup')],
                           check=True)
            subprocess.run([str(path / 'test_p1_bringup')], check=True)

    def test_nvmtl_vk_p1_bda_fence_rp_light_pcache_and_barriers(self):
        vk_h = (ROOT / 'src/nmplugin/nvmtl_vk.h').read_text()
        vk_c = (ROOT / 'src/nmplugin/nvmtl_vk.c').read_text()

        # Source invariants check
        self.assertIn('uint64_t bda;', vk_h)
        self.assertNotIn('nvmtl_opaque_CreateGraphicsPipelines(g_dev, VK_NULL_HANDLE', vk_c)
        self.assertNotIn('nvmtl_opaque_CreateComputePipelines(g_dev, VK_NULL_HANDLE', vk_c)
        self.assertNotIn('pvkCreateComputePipelines(g_dev, VK_NULL_HANDLE', vk_c)
        self.assertIn('nvmtl_pipe_cache_init();', vk_c)
        self.assertIn('nvmtl_fence_acquire(&fence)', vk_c)
        self.assertIn('nvmtl_fence_release(fence, 1)', vk_c)
        for fn in ('int nvmtl_vk_cmd_fill_buffer(', 'int nvmtl_vk_cmd_write_word(',
                   'int nvmtl_vk_cmd_copy_buffer(', 'int nvmtl_vk_cmd_copy_buffer_to_image_level_layer(',
                   'int nvmtl_vk_cmd_copy_image_to_buffer_level_layer('):
            body = method(vk_c, fn)
            self.assertIn('nvmtl_vk_cmd_barrier_to_transfer(', body, fn)
            self.assertIn('nvmtl_vk_cmd_barrier_from_transfer(', body, fn)
            self.assertNotIn('nvmtl_vk_cmd_barrier(', body, fn)

        extracted = '\n'.join([
            method(vk_c, 'static void nvmtl_pipe_cache_path('),
            method(vk_c, 'static void nvmtl_pipe_cache_save('),
            method(vk_c, 'static void nvmtl_pipe_cache_init('),
            method(vk_c, 'static void nvmtl_pipe_cache_note_compile('),
            method(vk_c, 'static VkResult nvmtl_fence_acquire('),
            method(vk_c, 'static void nvmtl_fence_release('),
            method(vk_c, 'static int nvmtl_rp_light('),
            method(vk_c, 'static void nvmtl_vk_cmd_barrier_to_transfer('),
            method(vk_c, 'static void nvmtl_vk_cmd_barrier_from_transfer('),
            method(vk_c, 'uint64_t nvmtl_vk_buffer_address('),
        ])

        c_harness = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <limits.h>
#ifndef PATH_MAX
#define PATH_MAX 4096
#endif
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

typedef int VkResult;
typedef uint32_t VkFlags;
typedef uint32_t VkBool32;
typedef struct VkDevice_T *VkDevice;
typedef struct VkBuffer_T *VkBuffer;
typedef struct VkFence_T *VkFence;
typedef struct VkPipelineCache_T *VkPipelineCache;
typedef struct VkCommandBuffer_T *VkCommandBuffer;
typedef void VkAllocationCallbacks;

#define VK_SUCCESS 0
#define VK_ERROR_INITIALIZATION_FAILED (-3)
#define VK_NULL_HANDLE NULL
#define VK_STRUCTURE_TYPE_FENCE_CREATE_INFO 8
#define VK_STRUCTURE_TYPE_PIPELINE_CACHE_CREATE_INFO 17
#define VK_STRUCTURE_TYPE_BUFFER_DEVICE_ADDRESS_INFO 1000244001
#define VK_STRUCTURE_TYPE_MEMORY_BARRIER 46
#define VK_ACCESS_MEMORY_READ_BIT 0x80u
#define VK_ACCESS_MEMORY_WRITE_BIT 0x100u
#define VK_ACCESS_TRANSFER_READ_BIT 0x800u
#define VK_ACCESS_TRANSFER_WRITE_BIT 0x1000u
#define VK_PIPELINE_STAGE_TRANSFER_BIT 0x1000u
#define VK_PIPELINE_STAGE_ALL_COMMANDS_BIT 0x10000u

typedef VkFlags VkPipelineStageFlags;
typedef VkFlags VkAccessFlags;

typedef struct { int sType; const void *pNext; VkFlags flags; } VkFenceCreateInfo;
typedef struct { int sType; const void *pNext; VkFlags flags; size_t initialDataSize; const void *pInitialData; } VkPipelineCacheCreateInfo;
typedef struct { int sType; const void *pNext; VkBuffer buffer; } VkBufferDeviceAddressInfo;
typedef struct { int sType; const void *pNext; VkAccessFlags srcAccessMask; VkAccessFlags dstAccessMask; } VkMemoryBarrier;
typedef struct { void *cb; } nvk_cmdbuf;
typedef struct { void *buf; uint64_t bda; } nvk_buffer;

typedef VkResult (*PFN_vkCreatePipelineCache)(VkDevice, const VkPipelineCacheCreateInfo *, const VkAllocationCallbacks *, VkPipelineCache *);
typedef void (*PFN_vkDestroyPipelineCache)(VkDevice, VkPipelineCache, const VkAllocationCallbacks *);
typedef VkResult (*PFN_vkGetPipelineCacheData)(VkDevice, VkPipelineCache, size_t *, void *);

static VkDevice g_dev = (VkDevice)0x1;
static _Atomic int g_state = 1;
static const char *g_prog = "WindowServer";
static uint16_t g_vendor_val = 0x1002;
static const char *getprogname(void) { return g_prog; }
static uint16_t nvmtl_vk_vendor(void) { return g_vendor_val; }
static void nvlog(const char *fmt, ...) { (void)fmt; }

static unsigned g_fence_creates = 0, g_fence_resets = 0, g_fence_destroys = 0;
static int g_fail_reset = 0;
static uint64_t g_next_fence_id = 0x100;
static VkResult mock_vkCreateFence(VkDevice dev, const VkFenceCreateInfo *ci, const VkAllocationCallbacks *alloc, VkFence *out) {
    (void)dev; (void)ci; (void)alloc;
    g_fence_creates++;
    *out = (VkFence)(uintptr_t)(++g_next_fence_id);
    return VK_SUCCESS;
}
static VkResult mock_vkResetFences(VkDevice dev, uint32_t count, const VkFence *fences) {
    (void)dev; (void)fences;
    assert(count == 1);
    if (g_fail_reset) return VK_ERROR_INITIALIZATION_FAILED;
    g_fence_resets++;
    return VK_SUCCESS;
}
static void mock_vkDestroyFence(VkDevice dev, VkFence fence, const VkAllocationCallbacks *alloc) {
    (void)dev; (void)fence; (void)alloc;
    g_fence_destroys++;
}
static VkResult (*pvkCreateFence)(VkDevice, const VkFenceCreateInfo *, const VkAllocationCallbacks *, VkFence *) = mock_vkCreateFence;
static VkResult (*pvkResetFences)(VkDevice, uint32_t, const VkFence *) = mock_vkResetFences;
static void (*pvkDestroyFence)(VkDevice, VkFence, const VkAllocationCallbacks *) = mock_vkDestroyFence;

static unsigned g_bda_calls = 0;
static uint64_t mock_vkGetBufferDeviceAddress(VkDevice dev, const VkBufferDeviceAddressInfo *info) {
    (void)dev;
    g_bda_calls++;
    return 0xbeef0000ull + (uint64_t)(uintptr_t)info->buffer;
}
static uint64_t (*pvkGetBufferDeviceAddress)(VkDevice, const VkBufferDeviceAddressInfo *) = mock_vkGetBufferDeviceAddress;

static VkPipelineStageFlags g_last_src_stage = 0, g_last_dst_stage = 0;
static VkMemoryBarrier g_last_mb = {0};
static void mock_vkCmdPipelineBarrier(VkCommandBuffer cb, VkPipelineStageFlags s, VkPipelineStageFlags d, VkFlags dep,
                                      uint32_t nmb, const VkMemoryBarrier *mb, uint32_t nbm, const void *bm, uint32_t nim, const void *im) {
    (void)cb; (void)dep; (void)nbm; (void)bm; (void)nim; (void)im;
    g_last_src_stage = s; g_last_dst_stage = d;
    if (nmb && mb) g_last_mb = *mb;
}
static void (*pvkCmdPipelineBarrier)(VkCommandBuffer, VkPipelineStageFlags, VkPipelineStageFlags, VkFlags,
                                     uint32_t, const VkMemoryBarrier *, uint32_t, const void *, uint32_t, const void *) = mock_vkCmdPipelineBarrier;

static size_t g_loaded_pcache_bytes = 0;
static uint8_t g_loaded_pcache_sample[64] = {0};
static uint8_t g_pcache_blob[8] = {'P', 'H', 'X', 'C', 1, 2, 3, 4};
static VkResult mock_vkCreatePipelineCache(VkDevice dev, const VkPipelineCacheCreateInfo *ci, const VkAllocationCallbacks *alloc, VkPipelineCache *out) {
    (void)dev; (void)alloc;
    g_loaded_pcache_bytes = ci->initialDataSize;
    if (ci->pInitialData && ci->initialDataSize) {
        size_t cp = ci->initialDataSize < sizeof(g_loaded_pcache_sample) ? ci->initialDataSize : sizeof(g_loaded_pcache_sample);
        memcpy(g_loaded_pcache_sample, ci->pInitialData, cp);
    }
    *out = (VkPipelineCache)0x5555;
    return VK_SUCCESS;
}
static void mock_vkDestroyPipelineCache(VkDevice dev, VkPipelineCache pc, const VkAllocationCallbacks *alloc) {
    (void)dev; (void)pc; (void)alloc;
}
static VkResult mock_vkGetPipelineCacheData(VkDevice dev, VkPipelineCache pc, size_t *sz, void *data) {
    (void)dev; (void)pc;
    *sz = sizeof(g_pcache_blob);
    if (data) memcpy(data, g_pcache_blob, sizeof(g_pcache_blob));
    return VK_SUCCESS;
}
static PFN_vkCreatePipelineCache pvkCreatePipelineCache = mock_vkCreatePipelineCache;
static PFN_vkDestroyPipelineCache pvkDestroyPipelineCache = mock_vkDestroyPipelineCache;
static PFN_vkGetPipelineCacheData pvkGetPipelineCacheData = mock_vkGetPipelineCacheData;

static VkPipelineCache g_pipe_cache;
static _Atomic uint64_t g_pipe_compiled;
static pthread_mutex_t g_pipe_cache_lock = PTHREAD_MUTEX_INITIALIZER;
static char g_pipe_cache_path[PATH_MAX];
#define NVMTL_FENCE_POOL_CAP 32
static VkFence g_fence_pool[NVMTL_FENCE_POOL_CAP];
static unsigned g_fence_pool_n;
static pthread_mutex_t g_fence_pool_lock = PTHREAD_MUTEX_INITIALIZER;

''' + extracted + r'''

static int check_rp_light_forked(const char *prog, uint16_t vendor, const char *env_val) {
    pid_t pid = fork();
    assert(pid >= 0);
    if (pid == 0) {
        g_prog = prog;
        g_vendor_val = vendor;
        if (env_val) setenv("NVMTL_RP_LIGHT", env_val, 1);
        else unsetenv("NVMTL_RP_LIGHT");
        int res = nvmtl_rp_light();
        _exit(res ? 10 : 20);
    }
    int st = 0;
    assert(waitpid(pid, &st, 0) == pid && WIFEXITED(st));
    return WEXITSTATUS(st) == 10 ? 1 : 0;
}

int main(void) {
    // 1. BDA cache hit avoids repeated driver queries.
    nvk_buffer buf = {0};
    buf.buf = (void *)0x1234;
    uint64_t a1 = nvmtl_vk_buffer_address(&buf);
    uint64_t a2 = nvmtl_vk_buffer_address(&buf);
    assert(a1 == 0xbeef0000ull + 0x1234 && a2 == a1 && g_bda_calls == 1);
    assert(nvmtl_vk_buffer_address(NULL) == 0);

    // 2. VkFence pool acquire/release, reset fail, unhealthy destroy, and cap limit.
    VkFence f1 = NULL, f2 = NULL;
    assert(nvmtl_fence_acquire(&f1) == VK_SUCCESS && g_fence_creates == 1);
    nvmtl_fence_release(f1, 1);
    assert(g_fence_resets == 1 && g_fence_destroys == 0);
    assert(nvmtl_fence_acquire(&f2) == VK_SUCCESS && f2 == f1 && g_fence_creates == 1);
    nvmtl_fence_release(f2, 0); // unhealthy -> destroyed, not pooled
    assert(g_fence_destroys == 1 && g_fence_pool_n == 0);
    g_fail_reset = 1;
    assert(nvmtl_fence_acquire(&f1) == VK_SUCCESS && g_fence_creates == 2);
    nvmtl_fence_release(f1, 1); // reset fails -> destroyed
    assert(g_fence_destroys == 2 && g_fence_pool_n == 0);
    g_fail_reset = 0;
    VkFence bulk[NVMTL_FENCE_POOL_CAP + 4];
    for (unsigned i = 0; i < NVMTL_FENCE_POOL_CAP + 4; i++)
        assert(nvmtl_fence_acquire(&bulk[i]) == VK_SUCCESS);
    for (unsigned i = 0; i < NVMTL_FENCE_POOL_CAP + 4; i++)
        nvmtl_fence_release(bulk[i], 1);
    assert(g_fence_pool_n == NVMTL_FENCE_POOL_CAP && g_fence_destroys == 6);

    // 3. Narrowed transfer barriers.
    nvk_cmdbuf cb = { (void *)0x1 };
    nvmtl_vk_cmd_barrier_to_transfer(&cb);
    assert(g_last_src_stage == VK_PIPELINE_STAGE_ALL_COMMANDS_BIT && g_last_dst_stage == VK_PIPELINE_STAGE_TRANSFER_BIT);
    assert(g_last_mb.dstAccessMask == (VK_ACCESS_TRANSFER_READ_BIT | VK_ACCESS_TRANSFER_WRITE_BIT));
    nvmtl_vk_cmd_barrier_from_transfer(&cb);
    assert(g_last_src_stage == VK_PIPELINE_STAGE_TRANSFER_BIT && g_last_dst_stage == VK_PIPELINE_STAGE_ALL_COMMANDS_BIT);
    assert(g_last_mb.srcAccessMask == VK_ACCESS_TRANSFER_WRITE_BIT);

    // 4. Persistent pipeline cache path sanitization + save + reload.
    char pbuf[256];
    nvmtl_pipe_cache_path(pbuf, sizeof(pbuf), 0x1002, "foo/bar baz", "/tmp/sub/", 501);
    assert(strcmp(pbuf, "/tmp/sub/nvmtl-pcache-501-1002-foo_bar_baz.bin") == 0);
    char tmproot[] = "/tmp/nvmtl_pcache_XXXXXX";
    assert(mkdtemp(tmproot) != NULL);
    setenv("TMPDIR", tmproot, 1);
    unsetenv("NVMTL_PIPE_CACHE");
    g_prog = "TestApp";
    nvmtl_pipe_cache_init();
    assert(g_pipe_cache != VK_NULL_HANDLE && g_loaded_pcache_bytes == 0);
    for (int i = 0; i < 8; i++) nvmtl_pipe_cache_note_compile(); // 8th compile triggers save
    g_pipe_cache = VK_NULL_HANDLE;
    nvmtl_pipe_cache_init(); // reloads saved blob
    assert(g_loaded_pcache_bytes == sizeof(g_pcache_blob));
    assert(memcmp(g_loaded_pcache_sample, g_pcache_blob, sizeof(g_pcache_blob)) == 0);
    g_pipe_cache = VK_NULL_HANDLE;
    unlink(g_pipe_cache_path);
    rmdir(tmproot);

    // 5. NVMTL_RP_LIGHT defaults ON for WindowServer on AMD 0x1002, honors NVMTL_RP_LIGHT=0.
    assert(check_rp_light_forked("WindowServer", 0x1002, NULL) == 1);
    assert(check_rp_light_forked("WindowServer", 0x1002, "0") == 0);
    assert(check_rp_light_forked("WindowServer", 0x10de, NULL) == 0);
    assert(check_rp_light_forked("GameApp", 0x1002, "1") == 0);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp)
            (path / 'test_p1_vk.c').write_text(c_harness)
            subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-pthread', '-fsanitize=address,undefined',
                            str(path / 'test_p1_vk.c'), '-o', str(path / 'test_p1_vk')],
                           check=True)
            subprocess.run([str(path / 'test_p1_vk')], check=True)


if __name__ == '__main__':
    unittest.main()
