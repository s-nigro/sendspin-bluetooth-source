#pragma once

#include "esphome/components/microphone/microphone_source.h"
#include "esphome/core/component.h"

#include <atomic>
#include <cstdint>
#include <string>
#include <vector>

#include <lwip/sockets.h>

namespace esphome {
namespace sendspin_latency_mic {

// Streams the microphone with timestamps to the Sendspin Bluetooth Source add-on
// while it measures speaker latency. Protocol: see btsource/calibration.py.
class LatencyMic : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_WIFI; }

  void set_microphone_source(microphone::MicrophoneSource *source) { this->mic_source_ = source; }
  void set_port(uint16_t port) { this->port_ = port; }
  void set_key(const std::string &key) { this->key_ = key; }

 protected:
  static void udp_task(void *arg);
  void handle_packet_(const uint8_t *buf, size_t len, const struct sockaddr_in &from, int64_t t_recv);
  bool check_key_(const uint8_t *&p, const uint8_t *end) const;
  void on_audio_(const uint8_t *data, size_t len, int64_t capture_time_us);

  microphone::MicrophoneSource *mic_source_{nullptr};
  uint16_t port_{8939};
  std::string key_;
  int rx_sock_{-1};
  int tx_sock_{-1};
  struct sockaddr_in peer_ {};
  std::atomic<bool> streaming_{false};
  std::atomic<bool> want_start_{false};
  std::atomic<bool> want_stop_{false};
  std::atomic<int64_t> stream_until_{0};
  uint32_t audio_seq_{0};
};

}  // namespace sendspin_latency_mic
}  // namespace esphome
