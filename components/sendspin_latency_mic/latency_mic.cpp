#include "latency_mic.h"

#include "esphome/core/log.h"
#include "esphome/core/macros.h"
#include "esphome/core/version.h"

#include <cstring>

#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

namespace esphome {
namespace sendspin_latency_mic {

static const char *const TAG = "sendspin_latency_mic";

static const uint8_t MAGIC[4] = {'S', 'L', 'M', '1'};
enum PacketType : uint8_t { PING = 1, PONG = 2, START = 3, STOP = 4, AUDIO = 5 };

static constexpr size_t MAX_SAMPLES_PER_PACKET = 600;  // 1200 bytes payload, below a typical MTU
static constexpr uint32_t MAX_STREAM_MS = 60000;

// ESP32 is little endian, like the wire format.
template<typename T> static void put(uint8_t *&p, T v) {
  memcpy(p, &v, sizeof(T));
  p += sizeof(T);
}
template<typename T> static bool get(const uint8_t *&p, const uint8_t *end, T &v) {
  if (end - p < static_cast<ptrdiff_t>(sizeof(T)))
    return false;
  memcpy(&v, p, sizeof(T));
  p += sizeof(T);
  return true;
}

void LatencyMic::setup() {
#if ESPHOME_VERSION_CODE >= VERSION_CODE(2026, 11, 0)
  // Since 2026.11 the microphone reports when the first frame was captured (from DMA timing).
  this->mic_source_->add_data_callback([this](const microphone::AudioCapture &capture) {
    this->on_audio_(capture.data.data(), capture.data.size(), capture.capture_time_us);
  });
#else
  this->mic_source_->add_data_callback(
      [this](const std::vector<uint8_t> &data) { this->on_audio_(data.data(), data.size(), 0); });
#endif

  this->rx_sock_ = ::socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
  this->tx_sock_ = ::socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
  if (this->rx_sock_ < 0 || this->tx_sock_ < 0) {
    ESP_LOGE(TAG, "Could not create UDP sockets");
    this->mark_failed();
    return;
  }
  struct sockaddr_in addr {};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(this->port_);
  addr.sin_addr.s_addr = htonl(INADDR_ANY);
  if (::bind(this->rx_sock_, reinterpret_cast<struct sockaddr *>(&addr), sizeof(addr)) < 0) {
    ESP_LOGE(TAG, "Could not bind UDP port %u", this->port_);
    this->mark_failed();
    return;
  }
  // A dedicated task answers pings immediately, so the clock sync is not blurred by
  // the main loop interval.
  xTaskCreate(&LatencyMic::udp_task, "latency_mic", 4096, this, 5, nullptr);
}

void LatencyMic::udp_task(void *arg) {
  auto *self = static_cast<LatencyMic *>(arg);
  uint8_t buf[256];
  while (true) {
    struct sockaddr_in from {};
    socklen_t from_len = sizeof(from);
    int n = ::recvfrom(self->rx_sock_, buf, sizeof(buf), 0, reinterpret_cast<struct sockaddr *>(&from), &from_len);
    int64_t t_recv = esp_timer_get_time();
    if (n > 0)
      self->handle_packet_(buf, static_cast<size_t>(n), from, t_recv);
    else
      vTaskDelay(pdMS_TO_TICKS(10));
  }
}

bool LatencyMic::check_key_(const uint8_t *&p, const uint8_t *end) const {
  uint8_t len;
  if (!get(p, end, len) || end - p < len)
    return false;
  bool ok = len == this->key_.size() && memcmp(p, this->key_.data(), len) == 0;
  p += len;
  return ok;
}

void LatencyMic::handle_packet_(const uint8_t *buf, size_t len, const struct sockaddr_in &from, int64_t t_recv) {
  const uint8_t *p = buf;
  const uint8_t *end = buf + len;
  if (len < 5 || memcmp(p, MAGIC, 4) != 0)
    return;
  uint8_t type = p[4];
  p += 5;
  if (!this->check_key_(p, end)) {
    ESP_LOGW(TAG, "Packet with wrong key ignored");
    return;
  }
  switch (type) {
    case PING: {
      uint32_t seq;
      uint64_t t_addon;
      if (!get(p, end, seq) || !get(p, end, t_addon))
        return;
      uint8_t out[5 + 4 + 8 + 8];
      uint8_t *o = out;
      memcpy(o, MAGIC, 4);
      o += 4;
      *o++ = PONG;
      put<uint32_t>(o, seq);
      put<uint64_t>(o, t_addon);
      put<uint64_t>(o, static_cast<uint64_t>(t_recv));
      ::sendto(this->rx_sock_, out, sizeof(out), 0, reinterpret_cast<const struct sockaddr *>(&from), sizeof(from));
      break;
    }
    case START: {
      uint32_t duration_ms;
      if (!get(p, end, duration_ms))
        return;
      if (duration_ms > MAX_STREAM_MS)
        duration_ms = MAX_STREAM_MS;
      this->peer_ = from;
      this->stream_until_ = esp_timer_get_time() + static_cast<int64_t>(duration_ms) * 1000;
      this->want_start_ = true;
      break;
    }
    case STOP:
      this->want_stop_ = true;
      break;
    default:
      break;
  }
}

void LatencyMic::loop() {
  if (this->want_start_.exchange(false)) {
    this->audio_seq_ = 0;
    this->streaming_ = true;
    this->mic_source_->start();
    ESP_LOGI(TAG, "Measurement started, streaming microphone");
  }
  if (this->streaming_ && (this->want_stop_.exchange(false) || esp_timer_get_time() > this->stream_until_)) {
    this->streaming_ = false;
    this->mic_source_->stop();
    ESP_LOGI(TAG, "Measurement finished");
  }
}

void LatencyMic::on_audio_(const uint8_t *data, size_t len, int64_t capture_time_us) {
  if (!this->streaming_ || len < 2)
    return;
  const uint32_t rate = this->mic_source_->get_audio_stream_info().get_sample_rate();
  if (rate == 0)
    return;
  const size_t samples = len / 2;
  // Prefer the microphone's own capture time; otherwise assume the block was just read,
  // i.e. its first sample was captured `samples` ago (misses the DMA buffering delay).
  const int64_t t_first = capture_time_us != 0
                              ? capture_time_us
                              : esp_timer_get_time() - static_cast<int64_t>(samples) * 1000000 / rate;

  uint8_t out[5 + 4 + 8 + 4 + 2 + MAX_SAMPLES_PER_PACKET * 2];
  for (size_t off = 0; off < samples; off += MAX_SAMPLES_PER_PACKET) {
    size_t n = samples - off < MAX_SAMPLES_PER_PACKET ? samples - off : MAX_SAMPLES_PER_PACKET;
    uint8_t *o = out;
    memcpy(o, MAGIC, 4);
    o += 4;
    *o++ = AUDIO;
    put<uint32_t>(o, this->audio_seq_++);
    put<int64_t>(o, t_first + static_cast<int64_t>(off) * 1000000 / rate);
    put<uint32_t>(o, rate);
    put<uint16_t>(o, static_cast<uint16_t>(n));
    memcpy(o, data + off * 2, n * 2);
    o += n * 2;
    ::sendto(this->tx_sock_, out, o - out, 0, reinterpret_cast<const struct sockaddr *>(&this->peer_),
             sizeof(this->peer_));
  }
}

void LatencyMic::dump_config() {
  ESP_LOGCONFIG(TAG, "Sendspin latency microphone:");
  ESP_LOGCONFIG(TAG, "  UDP port: %u", this->port_);
}

}  // namespace sendspin_latency_mic
}  // namespace esphome
