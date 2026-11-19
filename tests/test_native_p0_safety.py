"""Host unit tests for P0 UM790 Pro safety baseline and kernel stability fixes."""
import pathlib
import tempfile
import subprocess
import unittest

from test_native_bo_lifetime import method

ROOT = pathlib.Path(__file__).resolve().parents[1]


class NativeP0SafetyTest(unittest.TestCase):
    def test_4k_pagetable_hash_reserve_sleep_and_accel_cache(self):
        source = (ROOT / 'src/phxbringup/PhxBringup.cpp').read_text()
        code = r'''
#include <cassert>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include "phxbringup/PhxNativeABI.h"
#include "phxbringup/PhxGpuQueue.h"
#include "phxbringup/PhxRange.h"
#include "phxbringup/PhxPendingFences.h"

using IOReturn = int;
using IOByteCount = uint64_t;
using addr64_t = uint64_t;
constexpr int kIOReturnSuccess = 0, kIOReturnBadArgument = 1, kIOReturnNotReady = 2;
constexpr int kIOReturnVMError = 3, kIOReturnNoMemory = 4, kIOReturnNotFound = 5, kIOReturnTimeout = 6;
constexpr int kIODirectionInOut = 0, kIOMemoryPhysicallyContiguous = 1, kIOMemoryMapperNone = 0;
constexpr int IOPMAckImplied = 0;
constexpr uint64_t kPtAddrMask = 0x0000FFFFFFFFF000ull;
constexpr uint64_t kPteSysFlags = 0x77ull | (4ull << 48);
constexpr uint32_t kFirstUserVmid = 8, kNumUserVmids = 8, kMaxCtx = 96;

struct IOService { virtual ~IOService() = default; };
struct Lock { bool held = false; };
static Lock *gTrackedLock = nullptr;
static void IOLockLock(Lock *l) { assert(!l->held); l->held = true; }
static void IOLockUnlock(Lock *l) { assert(l->held); l->held = false; }
static void bzero(void *p, size_t n) { std::memset(p, 0, n); }
static unsigned gFlushes = 0;
static void flushLine(const volatile void *) { gFlushes++; }
template<class... T> static void IOLog(const char *, T...) {}
static void nanoseconds_to_absolutetime(uint64_t ns, uint64_t *out) { *out = ns; }
static uint64_t gNow = 10000000000ull;
static uint64_t mach_absolute_time() { return gNow; }
static int gHangMs = 3000;
static int kernel_task = 0;

static unsigned gHeapAllocs = 0;
static void *IOMallocZero(size_t n) { gHeapAllocs++; return std::calloc(1, n); }
static void IOFree(void *p, size_t) { assert(gHeapAllocs); gHeapAllocs--; std::free(p); }

static uint64_t gNextPhys = 0x100000ull;
static unsigned gPageAllocs = 0, gPageFrees = 0;
static bool gFailPageAlloc = false;

struct IOBufferMemoryDescriptor {
    uint8_t bytes[4096]{};
    uint64_t phys{0};
    unsigned refs{1};
    bool badSeg{false};
    static IOBufferMemoryDescriptor *inTaskWithPhysicalMask(int, int, uint32_t sz, uint64_t) {
        if (gTrackedLock) assert(!gTrackedLock->held);
        assert(sz == 4096);
        if (gFailPageAlloc) return nullptr;
        auto *b = new IOBufferMemoryDescriptor();
        std::memset(b->bytes, 0xaa, sizeof(b->bytes));
        b->phys = gNextPhys;
        gNextPhys += 0x2000;
        gPageAllocs++;
        return b;
    }
    uint64_t getPhysicalAddress() const { return phys; }
    void *getBytesNoCopy() { return bytes; }
    void retain() { refs++; }
    void release() { assert(refs); if (--refs == 0) { gPageFrees++; delete this; } }
};

struct FakeMd {
    uint64_t len{8192};
    uint64_t getLength() const { return len; }
    addr64_t getPhysicalSegment(uint64_t off, IOByteCount *segLen, int) const {
        *segLen = 4096;
        return 0x40000000ull + off;
    }
};
using IOMemoryDescriptor = FakeMd;

struct OSSymbol {
    const char *name;
    unsigned refs{1};
    static unsigned symAllocs, symFrees;
    static const OSSymbol *withCStringNoCopy(const char *s) {
        symAllocs++;
        return new OSSymbol{s, 1};
    }
    void release() const {
        auto *m = const_cast<OSSymbol *>(this);
        assert(m->refs);
        if (--m->refs == 0) { symFrees++; delete m; }
    }
};
unsigned OSSymbol::symAllocs = 0;
unsigned OSSymbol::symFrees = 0;

struct PhxSurfShareEsc {
    uint32_t version, surfaceID, plane, hClient, hSrcClient, hSrcMemory, status, pad;
    uint64_t size;
};

struct AccelService {
    unsigned refs{1};
    bool inactive{false};
    bool allowCall{true};
    uint32_t lastSurf{0};
    uint32_t lastPlane{0};
    static unsigned instancesAlive;
    static AccelService *nextMatch;
    AccelService() { instancesAlive++; }
    void retain() { refs++; }
    void release() {
        if (gTrackedLock) assert(!gTrackedLock->held);
        assert(refs);
        if (--refs == 0) { instancesAlive--; delete this; }
    }
    bool isInactive() const { return inactive; }
    static const char *serviceMatching(const char *s) { return s; }
    static AccelService *copyMatchingService(const char *) {
        if (gTrackedLock) assert(!gTrackedLock->held);
        if (!nextMatch) return nullptr;
        nextMatch->retain();
        return nextMatch;
    }
    IOReturn callPlatformFunction(const OSSymbol *sym, bool, void *arg, void *, void *cmd, void *) {
        assert(sym && std::strcmp(sym->name, "nvSurfVram") == 0);
        assert(reinterpret_cast<uintptr_t>(cmd) == 0xE3);
        if (!allowCall) return kIOReturnNotFound;
        auto *e = static_cast<PhxSurfShareEsc *>(arg);
        lastSurf = e->surfaceID;
        lastPlane = e->plane;
        e->status = 0;
        return kIOReturnSuccess;
    }
};
unsigned AccelService::instancesAlive = 0;
AccelService *AccelService::nextMatch = nullptr;

struct IOExternalMethodArguments {
    uint32_t scalarInputCount{2}, scalarOutputCount{1};
    uint32_t structureInputSize{0}, structureOutputSize{0};
    void *structureInputDescriptor{nullptr}, *structureOutputDescriptor{nullptr};
    uint64_t scalarInput[2]{}, scalarOutput[1]{};
};
static bool shape(const IOExternalMethodArguments *a, uint32_t si, uint32_t so, uint32_t sti, uint32_t sto) {
    return a->scalarInputCount == si && a->scalarOutputCount == so &&
           a->structureInputSize == sti && a->structureOutputSize == sto;
}

struct PhxBringup {
    static constexpr uint32_t kPtPageBytes = 4096, kPtMaxPages = 1024, kPtHashCap = 2048, kPtReserveCap = 516;
    struct PtPage { IOBufferMemoryDescriptor *md; uint64_t phys; uint64_t *kva; };
    PtPage ptReserve[kPtReserveCap]{};
    uint32_t ptReserveCount{0};
    struct VmCtx {
        IOBufferMemoryDescriptor **chunk;
        uint64_t *chunkPhys;
        uint64_t **chunkKva;
        uint64_t *hashPhys;
        uint16_t *hashIdx;
        uint64_t phys;
        uint32_t pages, used;
        bool busy;
        uint32_t hw;
    } vm[kMaxCtx]{};
    struct HwVmid { uint32_t ctx; uint64_t lastSeq, lastUse, binding; } hwv[8]{};
    using GpuQueue = PhxGpuQueue;
    GpuQueue gq[2]{};
    struct Aper { alignas(8) uint8_t mem[4096]{}; uintptr_t getVirtualAddress() { return reinterpret_cast<uintptr_t>(mem); } } aper;
    Aper *fbAper = &aper;
    bool mmioBuf{true};
    bool *mmio = &mmioBuf;
    Lock gpuMutex, accelMutex;
    Lock *gpuLock = &gpuMutex, *accelLock = &accelMutex;
    AccelService *cachedAccel = nullptr;
    const OSSymbol *surfVramSym = nullptr;
    bool stopped = false, isSleeping = false, vmFaulted = false, ready = true;
    uint64_t gSeq = 0;
    unsigned tlbFlushes = 0, killCalls = 0;

    bool isStopped() const { return __atomic_load_n(&stopped, __ATOMIC_ACQUIRE); }
    bool isGpuSleeping() const { return __atomic_load_n(&isSleeping, __ATOMIC_ACQUIRE); }
    bool vmHealthy() const { return !__atomic_load_n(&vmFaulted, __ATOMIC_ACQUIRE); }
    bool gpuReady() const { return ready; }
    VmCtx *vmCtx(uint32_t handle) { return handle && handle <= kMaxCtx && vm[handle - 1].busy ? &vm[handle - 1] : nullptr; }
    uint64_t gpuRetiredLocked() const { return gSeq; }
    bool vmFlushTlb(uint32_t) { assert(gpuLock->held); tlbFlushes++; return true; }
    void killWaves(uint32_t, bool) { killCalls++; }

    static bool vmCtxAllocArrays(VmCtx &c);
    static void vmCtxFreeArrays(VmCtx &c);
    static bool ptAllocZeroedPage(PtPage &out);
    void ptRefillReserve(uint32_t target);
    bool ptPopReserveLocked(PtPage &out);
    static void ptHashInsert(VmCtx &c, uint64_t phys, uint32_t idx);
    uint64_t *ptTable(const VmCtx &c, uint64_t phys) const;
    uint64_t ptTablePhys(VmCtx &c, uint32_t i);
    uint64_t *vmPte(VmCtx &c, uint64_t va, bool alloc);
    uint32_t vmidAlloc();
    bool vmidFree(uint32_t handle, uint64_t lastSeq);
    IOReturn vmMap(uint32_t handle, uint64_t va, IOMemoryDescriptor *md, uint64_t off, uint64_t size);
    IOReturn vmUnmap(uint32_t handle, uint64_t va, uint64_t size);
    uint64_t queueFence(const GpuQueue &q) const;
    void hangCheck();
    IOReturn setPowerState(unsigned long ordinal, IOService *whichDevice);
    AccelService *retainAccel(const OSSymbol **symOut);
    void cleanupAll() {
        if (cachedAccel) { cachedAccel->release(); cachedAccel = nullptr; }
        if (surfVramSym) { surfVramSym->release(); surfVramSym = nullptr; }
        for (uint32_t i = 0; i < ptReserveCount; i++) {
            if (ptReserve[i].md) { ptReserve[i].md->release(); ptReserve[i].md = nullptr; }
        }
        ptReserveCount = 0;
        for (VmCtx &c : vm) {
            if (c.chunk) {
                for (uint32_t i = 0; i < kPtMaxPages; i++)
                    if (c.chunk[i]) c.chunk[i]->release();
                vmCtxFreeArrays(c);
            }
            c = VmCtx{};
        }
    }
};

// Map IOService alias to AccelService inside IOAccelPhxNativeClient::doSurfGpuMark & retainAccel
struct IOAccelPhxNativeClient {
    PhxBringup *owner{nullptr};
    void *task{nullptr};
    IOReturn doSurfGpuMark(IOExternalMethodArguments *a);
};
'''
        sigs = (
            'bool PhxBringup::vmCtxAllocArrays(',
            'void PhxBringup::vmCtxFreeArrays(',
            'bool PhxBringup::ptAllocZeroedPage(',
            'void PhxBringup::ptRefillReserve(',
            'bool PhxBringup::ptPopReserveLocked(',
            'void PhxBringup::ptHashInsert(',
            'uint64_t *PhxBringup::ptTable(',
            'uint64_t PhxBringup::ptTablePhys(',
            'uint64_t *PhxBringup::vmPte(',
            'uint32_t PhxBringup::vmidAlloc(',
            'bool PhxBringup::vmidFree(',
            'IOReturn PhxBringup::vmMap(',
            'IOReturn PhxBringup::vmUnmap(',
            'uint64_t PhxBringup::queueFence(',
            'void PhxBringup::hangCheck(',
            'IOReturn PhxBringup::setPowerState(',
        )
        for s in sigs:
            code += method(source, s)

        # Inject AccelService-typed retainAccel & doSurfGpuMark using IOService -> AccelService alias cleanly
        code += method(source, 'IOService *PhxBringup::retainAccel(').replace('IOService', 'AccelService')
        code += method(source, 'IOReturn IOAccelPhxNativeClient::doSurfGpuMark(').replace('IOService', 'AccelService')

        code += r'''
int main() {
    PhxBringup owner;
    gTrackedLock = owner.gpuLock;

    // 1. Pre-fill reserve happens strictly outside gpuLock, obeys cap, and allocates 4 KiB pages.
    owner.ptRefillReserve(1000);
    assert(owner.ptReserveCount == PhxBringup::kPtReserveCap);
    assert(gPageAllocs == PhxBringup::kPtReserveCap && !gPageFrees);

    // 2. O(1) hash lookup across all 1024 4 KiB page tables in a context.
    uint32_t ctx = owner.vmidAlloc();
    assert(ctx == 1);
    auto &c = owner.vm[0];
    assert(c.phys != 0 && c.used == 1);
    assert(owner.ptTable(c, c.phys) == c.chunkKva[0]);
    assert(owner.ptTable(c, 0) == nullptr);
    assert(owner.ptTable(c, 0xdeadbeef000ull) == nullptr);

    // Refill reserve again and verify all 1024 table slots can be hashed & resolved cleanly.
    owner.ptRefillReserve(516);
    IOLockLock(owner.gpuLock);
    for (uint32_t i = 1; i < PhxBringup::kPtMaxPages; i++) {
        if (!owner.ptReserveCount) {
            IOLockUnlock(owner.gpuLock);
            owner.ptRefillReserve(516);
            IOLockLock(owner.gpuLock);
        }
        uint64_t pa = owner.ptTablePhys(c, i);
        assert(pa != 0);
        assert(owner.ptTable(c, pa) == c.chunkKva[i]);
    }
    IOLockUnlock(owner.gpuLock);

    // 3. Sleep guard prevents vmidAlloc / hangCheck MMIO / vmMap / vmUnmap.
    owner.setPowerState(0, nullptr);
    assert(owner.isGpuSleeping());
    assert(owner.vmidAlloc() == 0);
    owner.gq[0].ready = true; owner.gq[0].tail = 1; owner.gq[0].fseq[0] = 1;
    owner.gq[0].fvmid[0] = 8; owner.gq[0].fbinding[0] = 1; owner.gq[0].lastProgress = 0;
    owner.hwv[0].ctx = 1; owner.hwv[0].binding = 1; c.hw = 8;
    owner.hangCheck();
    assert(owner.killCalls == 0); // must not touch GPU while asleep

    owner.setPowerState(1, nullptr);
    assert(!owner.isGpuSleeping());

    // Map 8 KiB on context 2 and test hangCheck fires after wake, then unmap + free.
    FakeMd md;
    uint32_t ctx2 = owner.vmidAlloc();
    assert(ctx2 == 2);
    assert(owner.vmMap(ctx2, 0x100000000ull, &md, 0, 8192) == kIOReturnSuccess);
    IOLockLock(owner.gpuLock);
    uint64_t *pte = owner.vmPte(owner.vm[ctx2 - 1], 0x100000000ull, false);
    assert(pte && (*pte & kPtAddrMask) == 0x40000000ull);
    IOLockUnlock(owner.gpuLock);
    assert(owner.vmUnmap(ctx2, 0x100000000ull, 8192) == kIOReturnSuccess);
    assert(owner.vmidFree(ctx2, 0));

    // Free context 1 too (write fence 1 into aper).
    owner.gSeq = 1;
    volatile uint32_t *fw = reinterpret_cast<volatile uint32_t *>(owner.aper.mem);
    fw[0] = 1; fw[1] = 0;
    assert(owner.queueFence(owner.gq[0]) == 1);
    assert(owner.vmidFree(ctx, 1));

    // 4. retainAccel & doSurfGpuMark balanced retains and invalidation refresh.
    gTrackedLock = owner.accelLock;
    auto *a1 = new AccelService();
    AccelService::nextMatch = a1;
    IOAccelPhxNativeClient client;
    client.owner = &owner;
    IOExternalMethodArguments args{};
    args.scalarInput[0] = 42; args.scalarInput[1] = 1;
    assert(client.doSurfGpuMark(&args) == kIOReturnSuccess && args.scalarOutput[0] == 0);
    assert(a1->lastSurf == 42 && (a1->lastPlane & 1u) == 1);
    assert(a1->refs == 2); // caller released its retain, only owner.cachedAccel (+1) and test's a1 (+1) remain
    // Invalidate a1 -> next call swaps to a2 and releases a1 outside accelLock.
    auto *a2 = new AccelService();
    a1->inactive = true;
    AccelService::nextMatch = a2;
    args.scalarInput[0] = 43; args.scalarInput[1] = 0;
    assert(client.doSurfGpuMark(&args) == kIOReturnSuccess && a2->lastSurf == 43);
    a1->release(); // drops test ref -> deletes a1 now that owner dropped cachedAccel
    assert(AccelService::instancesAlive == 1);

    owner.cleanupAll();
    a2->release();
    assert(AccelService::instancesAlive == 0);
    assert(OSSymbol::symAllocs == OSSymbol::symFrees);
    assert(gPageAllocs == gPageFrees);
    assert(gHeapAllocs == 0);
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp)
            (path / 'test_bringup_p0.cpp').write_text(code)
            subprocess.run(['clang++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                            '-fsanitize=address,undefined', '-I' + str(ROOT / 'src'),
                            str(path / 'test_bringup_p0.cpp'), '-o', str(path / 'test_bringup_p0')],
                           check=True)
            subprocess.run([str(path / 'test_bringup_p0')], check=True)

    def test_nvrm_accel_surface_refs_and_iop_cache_and_ec_gate(self):
        source = (ROOT / 'src/phxaccel/accel/nvrm-accel.cpp').read_text()

        # Source invariants for P0 fixes
        self.assertNotIn('ml_io_map', source)
        self.assertEqual(source.count('static bool phxFirebatEcAllowed()'), 1)
        self.assertIn('phxFirebatEcAllowed()', source)
        self.assertIn('IODeviceMemory::withRange((IOPhysicalAddress)0xFE0B0400ull', source)
        self.assertLess(source.index('gCntIopCopySkip'), source.index('OSAddAtomic64(1, &gCntIopCopySkip);'))
        self.assertIn('if (!nvSurfWaitReady(s->getSurfaceID()))', source)
        self.assertIn('nvAccelIopCacheForget(this, nullptr, this);', source)
        self.assertIn('nvAccelIopCacheForget(nullptr, this, nullptr);', source)
        self.assertIn('nvAccelIopCacheForget(nullptr, this, this);', source)

        # Extract NVSurfPending + nvSurfMark + nvSurfWaitReady and NVIopFlipCache helpers
        idx_struct = source.index('struct NVSurfPending')
        end_wait = source.index('SYSCTL_INT(_debug, OID_AUTO, nvaccel_surface_gpu')
        surf_code = source[idx_struct:end_wait]

        iop_cache_code = (
            method(source, 'static void nvAccelIopCacheForget(const NVResource *res, const NVVidMemory *vm, const void *skipRelease)\n{') + '\n' +
            method(source, 'static bool nvAccelIopCacheGet(') + '\n' +
            method(source, 'static void nvAccelIopCachePut(') + '\n' +
            'template <typename T> struct NVScopedRetain {\n'
            '    T *ptr = nullptr;\n'
            '    ~NVScopedRetain() { if (ptr) ((OSObject *)ptr)->release(); }\n'
            '};\n'
        )

        harness = r'''
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <mutex>
#include <thread>

using SInt64 = int64_t;
using Boolean = bool;
constexpr int kMillisecondScale = 1;
constexpr int THREAD_UNINT = 0;

struct IOLock { std::mutex m; std::condition_variable cv; };
static IOLock *IOLockAlloc() { return new IOLock(); }
static void IOLockFree(IOLock *l) { delete l; }
static void IOLockLock(IOLock *l) { l->m.lock(); }
static void IOLockUnlock(IOLock *l) { l->m.unlock(); }
static void IOLockWakeup(IOLock *l, void *, bool) { l->cv.notify_all(); }
static uint64_t mach_absolute_time() {
    auto now = std::chrono::steady_clock::now().time_since_epoch();
    return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(now).count());
}
static void clock_interval_to_deadline(uint32_t ms, int, uint64_t *res) {
    *res = mach_absolute_time() + static_cast<uint64_t>(ms) * 1000000ull;
}
static void absolutetime_to_nanoseconds(uint64_t dt, uint64_t *ns) { *ns = dt; }
static void IOLockSleepDeadline(IOLock *l, void *, uint64_t deadline, int) {
    std::unique_lock<std::mutex> lk(l->m, std::adopt_lock);
    uint64_t now = mach_absolute_time();
    if (now < deadline)
        l->cv.wait_for(lk, std::chrono::nanoseconds(deadline - now));
    lk.release();
}

struct IOSimpleLock { bool held = false; };
static IOSimpleLock *IOSimpleLockAlloc() { return new IOSimpleLock(); }
static void IOSimpleLockFree(IOSimpleLock *l) { delete l; }
static void IOSimpleLockLock(IOSimpleLock *l) { assert(!l->held); l->held = true; }
static void IOSimpleLockUnlock(IOSimpleLock *l) { assert(l->held); l->held = false; }

static bool OSCompareAndSwapPtr(void *oldVal, void *newVal, void *volatile *addr) {
    void *cur = *addr;
    if (cur == oldVal) { *addr = newVal; return true; }
    return false;
}
static SInt64 OSAddAtomic64(SInt64 delta, volatile SInt64 *dst) {
    return __atomic_fetch_add(dst, delta, __ATOMIC_RELAXED);
}

static IOSimpleLock *gCheckSimpleLock = nullptr;
struct OSObject {
    unsigned refs{1};
    static unsigned totalReleases;
    virtual ~OSObject() = default;
    void retain() { refs++; }
    void release() {
        if (gCheckSimpleLock) assert(!gCheckSimpleLock->held);
        assert(refs);
        totalReleases++;
        refs--;
    }
};
unsigned OSObject::totalReleases = 0;

struct IOSurface : OSObject {};
struct NVResource : OSObject {};
struct NVVidMemory : OSObject {
    void *fRmHandle = reinterpret_cast<void *>(0x1234);
    unsigned long long fBytes = 4096;
    bool fScanout = false;
};

static int gSurfGpuOn = 1;
static SInt64 gCntSurfMarks = 0, gCntSurfWaits = 0, gCntSurfTimeouts = 0;
static int gSurfWaitMaxUs = 0;
static const unsigned kSurfGpuWaitMs = 40;
''' + surf_code + r'''
struct NVIopFlipCache { IOSurface *s; NVResource *res; NVVidMemory *vm; void *kmem; unsigned long long bytes; unsigned long long use; };
static NVIopFlipCache gIopFlipCache[8];
static IOSimpleLock *gIopFlipCacheLock = nullptr;
static unsigned long long gIopFlipCacheTick = 0;
''' + iop_cache_code + r'''
int main() {
    // 1. NVSurfPending refcounting: 2 marks require 2 unmarks before WaitReady unblocks.
    nvSurfMark(100, true);
    nvSurfMark(100, true);
    assert(gSurfPend[0].id == 100 && gSurfPend[0].refs == 2);
    nvSurfMark(100, false);
    assert(gSurfPend[0].id == 100 && gSurfPend[0].refs == 1);

    std::thread releaser([&] {
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
        nvSurfMark(100, false);
    });
    assert(nvSurfWaitReady(100) == true);
    releaser.join();
    assert(gCntSurfWaits == 1 && !gCntSurfTimeouts);

    // Timeout resets slot & refs and returns false.
    nvSurfMark(200, true);
    nvSurfMark(200, true);
    assert(nvSurfWaitReady(200) == false);
    assert(gCntSurfTimeouts == 1);
    for (const auto &e : gSurfPend) assert(e.id != 200 && e.refs == 0);

    // 2. gIopFlipCache refcounts res/vm/s and drops outside IOSimpleLock.
    IOSurface s[9];
    NVResource res[9];
    NVVidMemory vm[9];
    for (int i = 0; i < 8; i++)
        nvAccelIopCachePut(&s[i], &res[i], &vm[i], vm[i].fRmHandle, 4096);
    gCheckSimpleLock = gIopFlipCacheLock;
    for (int i = 0; i < 8; i++)
        assert(s[i].refs == 2 && res[i].refs == 2 && vm[i].refs == 2);

    // 9th put evicts slot 0 outside IOSimpleLock.
    nvAccelIopCachePut(&s[8], &res[8], &vm[8], vm[8].fRmHandle, 4096);
    assert(s[0].refs == 1 && res[0].refs == 1 && vm[0].refs == 1);

    // Cache hit retains caller's res/vm; concurrent forget leaves caller's scoped hold valid.
    {
        NVScopedRetain<NVResource> resHold;
        NVScopedRetain<NVVidMemory> vmHold;
        NVResource *rOut = nullptr; NVVidMemory *vOut = nullptr; void *kOut = nullptr; unsigned long long bOut = 0;
        assert(nvAccelIopCacheGet(&s[1], &rOut, &vOut, &kOut, &bOut));
        resHold.ptr = rOut; vmHold.ptr = vOut;
        assert(res[1].refs == 3 && vm[1].refs == 3);
        nvAccelIopCacheForget(nullptr, &vm[1], nullptr);
        assert(s[1].refs == 1 && res[1].refs == 2 && vm[1].refs == 2);
    }
    assert(res[1].refs == 1 && vm[1].refs == 1);

    // Skip-self forget in destructor prevents recursive double release.
    nvAccelIopCacheForget(&res[2], nullptr, &res[2]);
    assert(s[2].refs == 1 && res[2].refs == 2 && vm[2].refs == 1);

    nvSurfLock();
    IOLockFree(gSurfPendLock);
    IOSimpleLockFree(gIopFlipCacheLock);
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp)
            (path / 'test_nvrm_p0.cpp').write_text(harness)
            subprocess.run(['clang++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                            '-pthread', '-fsanitize=address,undefined',
                            str(path / 'test_nvrm_p0.cpp'), '-o', str(path / 'test_nvrm_p0')],
                           check=True)
            subprocess.run([str(path / 'test_nvrm_p0')], check=True)

    def test_phxfb_stop_cancels_vbl_before_freeing_state(self):
        source = (ROOT / 'src/phxaccel/accel/phxfb.cpp').read_text()
        vbl_src = method(source, 'static void vblFire(')
        stop_src = method(source, 'void PhxAMDFB::stop(')

        code = r'''
#include <cassert>
#include <cstdint>
#include <vector>
#include <string>

static std::vector<std::string> gLog;
using SInt64 = int64_t;
using thread_call_param_t = void *;
using thread_call_t = void *;
using IOFBInterruptProc = void (*)(void *, void *);
struct IOService {};
struct IOFramebuffer { static void stop(IOService *) { gLog.push_back("super::stop"); } };
struct Res {
    const char *tag;
    void terminate() { gLog.push_back(std::string("term:") + tag); }
    void release() { gLog.push_back(std::string("rel:") + tag); }
};
#define OSSafeReleaseNULL(x) do { if (x) { (x)->release(); (x) = nullptr; } } while (0)
static bool gVblStaged = false;
static uint64_t gPhxVblLastAbs = 0, gPhxVblSeq = 0;
static int64_t gVblLateUsSum = 0, gVblLateUsMax = 0, gVblN = 0;
static Res *gPhxHDMI = nullptr;
static void *gPhxPrimaryFB = nullptr;
static uint64_t gPhxFrameAbs = 0;
static bool gAliasSysctl = false;
static int *kFbOids[1]{};
static void sysctl_unregister_oid(int *) {}
static void phxAgdcRemove() { gLog.push_back("agdc"); }
static uint64_t mach_absolute_time() { return 1000000; }
static void nanoseconds_to_absolutetime(uint64_t ns, uint64_t *out) { *out = ns; }
static void absolutetime_to_nanoseconds(uint64_t dt, uint64_t *out) { *out = dt; }
static void OSAddAtomic64(int64_t v, int64_t *d) { *d += v; }
static void IOSleep(unsigned) {}
static unsigned gRearms = 0;
static void thread_call_enter_delayed(void *, uint64_t) { gRearms++; }
static void thread_call_cancel(void *) { gLog.push_back("vbl_cancel"); }
static bool thread_call_free(void *) { gLog.push_back("vbl_free"); return true; }

struct WorkLoop { void removeEventSource(Res *) { gLog.push_back("rm_wake"); } } gWl;
using IOWorkLoop = WorkLoop;

struct PhxAMDFB : IOFramebuffer {
    int dummyTimer = 1;
    thread_call_t fVblTimer = &dummyTimer;
    volatile bool fStopping = false;
    bool fBlDark = false;
    uint64_t fFrameNs = 8333333, fVblNext = 0;
    struct Irq { IOFBInterruptProc proc = nullptr; void *target = nullptr; void *ref = nullptr; } fVbl[1]{};
    Res resBl{"backlight"}, resWg{"wakegate"}, resAm{"aliasmap"}, resVa{"va"}, resMm{"mmio"}, resSf{"surfmap"};
    Res *fBacklight = &resBl, *fWakeGate = &resWg, *fAliasMap = &resAm, *fVa = &resVa, *fMmio = &resMm, *fSurfMap = &resSf;
    WorkLoop *getWorkLoop() { return &gWl; }
    void poolFree() { gLog.push_back("poolFree"); }
    void PMstop() { gLog.push_back("PMstop"); }
    void stop(IOService *provider);
''' + vbl_src + r'''
};
''' + stop_src + r'''
int main() {
    PhxAMDFB fb;
    gPhxPrimaryFB = &fb;
    PhxAMDFB::vblFire(&fb, nullptr);
    assert(gRearms == 1);

    fb.stop(nullptr);
    PhxAMDFB::vblFire(&fb, nullptr);
    assert(gRearms == 1); // stopped vblFire must not re-arm timer

    int cancelIdx = -1, freeIdx = -1, poolIdx = -1, mmioIdx = -1;
    for (int i = 0; i < static_cast<int>(gLog.size()); i++) {
        if (gLog[i] == "vbl_cancel") cancelIdx = i;
        if (gLog[i] == "vbl_free") freeIdx = i;
        if (gLog[i] == "poolFree") poolIdx = i;
        if (gLog[i] == "rel:mmio") mmioIdx = i;
    }
    assert(cancelIdx == 0 && freeIdx == 1);
    assert(freeIdx < poolIdx && poolIdx < mmioIdx);
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp)
            (path / 'test_phxfb_p0.cpp').write_text(code)
            subprocess.run(['clang++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                            '-fsanitize=address,undefined',
                            str(path / 'test_phxfb_p0.cpp'), '-o', str(path / 'test_phxfb_p0')],
                           check=True)
            subprocess.run([str(path / 'test_phxfb_p0')], check=True)


if __name__ == '__main__':
    unittest.main()
