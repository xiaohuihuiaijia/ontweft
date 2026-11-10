package local.ontology.cdc;

import org.junit.jupiter.api.Test;
import org.apache.kafka.connect.data.Schema;
import org.apache.kafka.connect.data.SchemaBuilder;
import org.apache.kafka.connect.data.Struct;
import org.apache.kafka.connect.data.Decimal;
import org.apache.kafka.connect.header.ConnectHeaders;
import org.apache.kafka.connect.source.SourceRecord;
import java.math.BigDecimal;
import java.util.Map;
import static org.junit.jupiter.api.Assertions.*;

final class EnvelopeCodecTest {
    private static final String GENERATION = "00000000-0000-0000-0000-000000000001";
    @Test void preservesSchemaPrecisionHeadersTombstoneAndStableIdentity() {
        Schema keySchema = SchemaBuilder.struct().name("key").field("id", Schema.INT64_SCHEMA).field("标签", Schema.STRING_SCHEMA).build();
        Struct key = new Struct(keySchema).put("id", 9007199254740993L).put("标签", "中文");
        Schema amount = Decimal.schema(18);
        Schema valueSchema = SchemaBuilder.struct().name("value")
                .field("op", Schema.STRING_SCHEMA).field("amount", amount)
                .field("bytes", Schema.BYTES_SCHEMA).field("ts_ms", Schema.INT64_SCHEMA).build();
        Struct value = new Struct(valueSchema).put("op", "u").put("amount", new BigDecimal("123456789012.123456789012345678"))
                .put("bytes", new byte[]{0, -1}).put("ts_ms", 1L);
        var headers = new ConnectHeaders().addString("repeat", "a").addString("repeat", "b");
        SourceRecord record = new SourceRecord(Map.of("server", "source"), Map.of("lsn", 9007199254740995L, "lsn_proc", 10L),
                "source.public.items", null, keySchema, key, valueSchema, value, 111L, headers);
        try (EnvelopeCodec codec = new EnvelopeCodec(GENERATION)) {
            var event = codec.encode(record);
            assertEquals(9007199254740993L, event.path("key").path("payload").path("id").asLong());
            assertEquals("AP8=", event.path("value").path("payload").path("bytes").asText());
            assertEquals("bytes", event.path("value").path("schema").path("fields").get(1).path("type").asText());
            assertEquals(2, event.path("headers").size());
            SourceRecord replay = new SourceRecord(Map.of("server", "source"), Map.of("lsn", 9007199254740995L, "lsn_proc", 99L),
                    record.topic(), null, keySchema, key, valueSchema, value, 999L, headers);
            assertEquals(event.path("event_id"), codec.encode(replay).path("event_id"));
            assertEquals(EnvelopeCodec.sha256(event.path("identity_material").asText()), event.path("event_id").asText());
            var tombstone = codec.encode(new SourceRecord(record.sourcePartition(), record.sourceOffset(), record.topic(), keySchema, key, null, null));
            assertEquals("tombstone", tombstone.path("event_kind").asText());
            assertTrue(tombstone.path("value").path("payload").isNull());
            assertNotEquals(event.path("event_id"), tombstone.path("event_id"));
            // The vector is transport data, intentionally no source credentials.
            try { java.nio.file.Files.writeString(java.nio.file.Path.of("target/identity-vector.json"), event.toPrettyString()); }
            catch (Exception e) { fail(e); }
        }
    }

    @Test void transactionBeginEndAndRowOrdinalsRemainDistinct() {
        Schema schema = SchemaBuilder.struct().field("id", Schema.STRING_SCHEMA).field("status", Schema.STRING_SCHEMA).build();
        try (EnvelopeCodec codec = new EnvelopeCodec(GENERATION)) {
            var begin = codec.encode(new SourceRecord(Map.of("server", "s"), Map.of("lsn", 1L), "s.transaction", null, null, schema,
                    new Struct(schema).put("id", "20:30").put("status", "BEGIN")));
            var end = codec.encode(new SourceRecord(Map.of("server", "s"), Map.of("lsn", 1L), "s.transaction", null, null, schema,
                    new Struct(schema).put("id", "20:30").put("status", "END")));
            assertNotEquals(begin.path("event_id"), end.path("event_id"));
            assertEquals("BEGIN", begin.path("transaction").path("status").asText());
        }
    }

    @Test void nativeInitialNotificationRetainsItsPreSnapshotEmptyOffset() {
        Schema keySchema = SchemaBuilder.struct().name("io.debezium.connector.common.NotificationKey")
                .field("id", Schema.STRING_SCHEMA).build();
        Schema valueSchema = SchemaBuilder.struct().name("io.debezium.connector.common.Notification")
                .field("id", Schema.STRING_SCHEMA).field("type", Schema.STRING_SCHEMA)
                .field("aggregate_type", Schema.STRING_SCHEMA).build();
        var key = new Struct(keySchema).put("id", "native-control-id");
        var value = new Struct(valueSchema).put("id", "native-control-id").put("type", "STARTED")
                .put("aggregate_type", "Initial Snapshot");
        try (EnvelopeCodec codec = new EnvelopeCodec(GENERATION)) {
            var record = new SourceRecord(Map.of("server", "source"), Map.of(), "source.__ontology_snapshot_notifications",
                    keySchema, key, valueSchema, value);
            var encoded = codec.encode(record);
            assertEquals("control", encoded.path("event_kind").asText());
            assertTrue(encoded.path("source_offset").isEmpty());
            assertEquals("STARTED", encoded.path("value").path("payload").path("type").asText());
            assertEquals(encoded.path("event_id"), codec.encode(record).path("event_id"));
            assertThrows(IllegalArgumentException.class, () -> codec.encode(new SourceRecord(
                    Map.of("server", "source"), Map.of(), "source.table", keySchema, key, null, null)));
        }
    }
}
