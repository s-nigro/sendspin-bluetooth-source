"""Stream an ESPHome microphone with timestamps to the Sendspin Bluetooth Source add-on.

Only while the add-on measures latency (START with the shared key); otherwise idle.
"""

import esphome.codegen as cg
from esphome.components import microphone
import esphome.config_validation as cv
from esphome.const import CONF_ID, CONF_MICROPHONE, CONF_PORT

CODEOWNERS = ["@s-nigro"]
DEPENDENCIES = ["network", "microphone"]

CONF_KEY = "key"

sendspin_latency_mic_ns = cg.esphome_ns.namespace("sendspin_latency_mic")
LatencyMic = sendspin_latency_mic_ns.class_("LatencyMic", cg.Component)

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(LatencyMic),
        cv.Optional(CONF_MICROPHONE, default={}): microphone.microphone_source_schema(
            min_bits_per_sample=16,
            max_bits_per_sample=16,
            min_channels=1,
            max_channels=1,
        ),
        cv.Optional(CONF_PORT, default=8939): cv.port,
        cv.Required(CONF_KEY): cv.All(cv.string_strict, cv.Length(min=8, max=64)),
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    mic_source = await microphone.microphone_source_to_code(config[CONF_MICROPHONE])
    cg.add(var.set_microphone_source(mic_source))
    cg.add(var.set_port(config[CONF_PORT]))
    cg.add(var.set_key(config[CONF_KEY]))
