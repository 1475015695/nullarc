#ifndef PHX_VA_MAPS_H
#define PHX_VA_MAPS_H

#include <stdint.h>

struct PhxVaMap {
    bool used;
    uint32_t handle;
    uint64_t va, size, off;
    uint32_t flags;
};

inline int phxVaMapLowerBound(const PhxVaMap *maps, int count, uint64_t va) {
    int lo = 0, hi = count;
    while (lo < hi) {
        const int mid = lo + (hi - lo) / 2;
        if (maps[mid].va < va)
            lo = mid + 1;
        else
            hi = mid;
    }
    return lo;
}

inline bool phxVaMapOverlaps(const PhxVaMap *maps, int count, uint64_t va, uint64_t size, int *insertPosOut) {
    const int pos = phxVaMapLowerBound(maps, count, va);
    if (insertPosOut)
        *insertPosOut = pos;
    if (pos > 0 && maps[pos - 1].used && maps[pos - 1].size > va - maps[pos - 1].va)
        return true;
    if (pos < count && maps[pos].used && maps[pos].va - va < size)
        return true;
    return false;
}

inline bool phxVaMapInsertAt(PhxVaMap *maps, int &count, int cap, int pos, const PhxVaMap &entry) {
    if (!maps || count < 0 || count >= cap || pos < 0 || pos > count)
        return false;
    for (int i = count; i > pos; i--)
        maps[i] = maps[i - 1];
    maps[pos] = entry;
    count++;
    return true;
}

inline void phxVaMapRemoveAt(PhxVaMap *maps, int &count, int pos) {
    if (!maps || pos < 0 || pos >= count)
        return;
    for (int i = pos; i + 1 < count; i++)
        maps[i] = maps[i + 1];
    count--;
    maps[count] = PhxVaMap{};
}

inline int phxVaMapFindExact(const PhxVaMap *maps, int count, uint32_t handle, uint64_t va, uint64_t size) {
    if (!maps || count <= 0)
        return -1;
    const int pos = phxVaMapLowerBound(maps, count, va);
    if (pos < count && maps[pos].used && maps[pos].handle == handle && maps[pos].va == va && maps[pos].size == size)
        return pos;
    return -1;
}

inline int phxVaMapFindContaining(const PhxVaMap *maps, int count, uint64_t va) {
    if (!maps || count <= 0)
        return -1;
    const int pos = phxVaMapLowerBound(maps, count, va);
    if (pos < count && maps[pos].used && maps[pos].va == va && maps[pos].size > 0)
        return pos;
    if (pos > 0 && maps[pos - 1].used && va - maps[pos - 1].va < maps[pos - 1].size)
        return pos - 1;
    return -1;
}

inline bool phxVaMapCovered(const PhxVaMap *maps, int count, uint64_t va, uint64_t bytes, uint32_t reqFlag) {
    if (bytes == 0)
        return true;
    if (!maps || count <= 0 || va + bytes < va)
        return false;
    uint64_t cur = va;
    const uint64_t end = va + bytes;
    int idx = phxVaMapFindContaining(maps, count, cur);
    while (cur < end) {
        if (idx < 0 || idx >= count)
            return false;
        const PhxVaMap &mp = maps[idx];
        if (!mp.used || (mp.flags & reqFlag) != reqFlag || cur < mp.va || cur - mp.va >= mp.size)
            return false;
        const uint64_t mpEnd = mp.va + mp.size;
        if (mpEnd >= end)
            return true;
        cur = mpEnd;
        idx++;
    }
    return true;
}

#endif
