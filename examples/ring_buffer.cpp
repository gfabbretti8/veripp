// A fixed-capacity ring buffer with contracts: should VERIFY.
#include "veripp/contracts.hpp"

class RingBuffer {
    int data_[8];
    unsigned head_ = 0, tail_ = 0, size_ = 0;

public:
    static constexpr unsigned capacity = 8;

    bool push(int v) {
        if (size_ == capacity) return false;
        data_[tail_] = v;
        tail_ = (tail_ + 1) % capacity;
        ++size_;
        VERIPP_ENSURES(size_ <= capacity);
        return true;
    }

    bool pop(int& out) {
        if (size_ == 0) return false;
        out = data_[head_];
        head_ = (head_ + 1) % capacity;
        --size_;
        VERIPP_ENSURES(size_ < capacity);
        return true;
    }

    unsigned size() const { return size_; }
};

#if defined(VERIPP_HAS_OWN_MAIN)
int main() {
    RingBuffer rb;
    for (int step = 0; step < 6; ++step) {
        if (VERIPP_NONDET_BOOL()) {
            rb.push(VERIPP_NONDET_INT());
        } else {
            int v;
            rb.pop(v);
        }
        VERIPP_ASSERT(rb.size() <= RingBuffer::capacity);
    }
    return 0;
}
#endif
