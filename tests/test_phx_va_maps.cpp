#include "phxbringup/PhxVaMaps.h"
#include <cassert>
#include <cstdint>
#include <limits>
#include <vector>

int main() {
    std::vector<PhxVaMap> maps(16, PhxVaMap{});
    int count = 0;
    int pos = -1;

    // 1. Empty interval checks.
    assert(phxVaMapLowerBound(maps.data(), count, 0x10000) == 0);
    assert(!phxVaMapOverlaps(maps.data(), count, 0x10000, 4096, &pos) && pos == 0);
    assert(phxVaMapFindExact(maps.data(), count, 1, 0x10000, 4096) == -1);
    assert(phxVaMapFindContaining(maps.data(), count, 0x10000) == -1);
    assert(phxVaMapCovered(maps.data(), count, 0x10000, 0, 1));
    assert(!phxVaMapCovered(maps.data(), count, 0x10000, 4096, 1));

    // 2. Sorted insert out-of-order and adjacency checks.
    assert(phxVaMapInsertAt(maps.data(), count, 16, 0, PhxVaMap{true, 2, 0x40000, 0x10000, 0, 7}));
    assert(!phxVaMapOverlaps(maps.data(), count, 0x10000, 0x10000, &pos) && pos == 0);
    assert(phxVaMapInsertAt(maps.data(), count, 16, pos, PhxVaMap{true, 1, 0x10000, 0x10000, 0, 7}));
    // Adjacent [0x20000, 0x40000) fits cleanly between [0x10000, 0x20000) and [0x40000, 0x50000).
    assert(!phxVaMapOverlaps(maps.data(), count, 0x20000, 0x20000, &pos) && pos == 1);
    assert(phxVaMapInsertAt(maps.data(), count, 16, pos, PhxVaMap{true, 3, 0x20000, 0x20000, 0, 3}));
    assert(count == 3);
    assert(maps[0].va == 0x10000 && maps[1].va == 0x20000 && maps[2].va == 0x40000);

    // 3. Overlap edge cases (start, middle, tail, enclosing).
    assert(phxVaMapOverlaps(maps.data(), count, 0x10000, 4096, nullptr));
    assert(phxVaMapOverlaps(maps.data(), count, 0x14000, 4096, nullptr));
    assert(phxVaMapOverlaps(maps.data(), count, 0x1f000, 8192, nullptr));
    assert(phxVaMapOverlaps(maps.data(), count, 0x0e000, 0x60000, nullptr));
    assert(!phxVaMapOverlaps(maps.data(), count, 0x50000, 4096, &pos) && pos == 3);

    // 4. Binary search lookup & multi-segment coverage.
    assert(phxVaMapFindExact(maps.data(), count, 3, 0x20000, 0x20000) == 1);
    assert(phxVaMapFindExact(maps.data(), count, 99, 0x20000, 0x20000) == -1);
    assert(phxVaMapFindExact(maps.data(), count, 3, 0x20000, 0x10000) == -1);
    assert(phxVaMapFindContaining(maps.data(), count, 0x25000) == 1);
    assert(phxVaMapFindContaining(maps.data(), count, 0x3ffff) == 1);
    assert(phxVaMapFindContaining(maps.data(), count, 0x50000) == -1);

    // Adjacent interval coverage: bit 1 set on both [0x10000..0x20000) and [0x20000..0x40000),
    // but bit 4 (exec) only set on [0x10000..0x20000) and [0x40000..0x50000).
    assert(phxVaMapCovered(maps.data(), count, 0x18000, 0x10000, 2));
    assert(!phxVaMapCovered(maps.data(), count, 0x18000, 0x10000, 4));
    assert(phxVaMapCovered(maps.data(), count, 0x18000, 0x08000, 4));
    assert(!phxVaMapCovered(maps.data(), count, 0x48000, 0x10000, 4));

    // Overflow guard at end of address space.
    assert(!phxVaMapCovered(maps.data(), count, std::numeric_limits<uint64_t>::max() - 4095, 8192, 1));

    // 5. Remove preserves sorted invariant and zeroes slot.
    phxVaMapRemoveAt(maps.data(), count, 1);
    assert(count == 2 && maps[0].va == 0x10000 && maps[1].va == 0x40000);
    assert(!maps[2].used);
    assert(!phxVaMapCovered(maps.data(), count, 0x18000, 0x30000, 2));

    // 6. Capacity 4096 stress test.
    constexpr int kCap = 4096;
    std::vector<PhxVaMap> full(kCap, PhxVaMap{});
    int fullCount = 0;
    for (int i = kCap - 1; i >= 0; i--) {
        const uint64_t va = 0x100000ull + static_cast<uint64_t>(i) * 0x2000ull;
        assert(!phxVaMapOverlaps(full.data(), fullCount, va, 0x1000ull, &pos));
        assert(pos == 0);
        assert(phxVaMapInsertAt(full.data(), fullCount, kCap, pos, PhxVaMap{true, 1, va, 0x1000ull, 0, 1}));
    }
    assert(fullCount == kCap);
    assert(!phxVaMapInsertAt(full.data(), fullCount, kCap, 0, PhxVaMap{true, 1, 0x1000, 0x1000, 0, 1}));
    assert(phxVaMapFindContaining(full.data(), fullCount, 0x100000ull + 2048ull * 0x2000ull + 512) == 2048);
    assert(phxVaMapFindContaining(full.data(), fullCount, 0x100000ull + 2048ull * 0x2000ull + 0x1500ull) == -1);
    return 0;
}
