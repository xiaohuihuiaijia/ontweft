package local.ontology.cdc;

import com.sun.net.httpserver.HttpServer;
import io.debezium.engine.DebeziumEngine;
import io.debezium.engine.RecordChangeEvent;
import org.apache.kafka.connect.data.Schema;
import org.apache.kafka.connect.source.SourceRecord;
import org.junit.jupiter.api.Test;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.AtomicInteger;
import static org.junit.jupiter.api.Assertions.*;

final class DurableHttpConsumerTest {
    private static final String GENERATION = "00000000-0000-0000-0000-000000000001";
    @Test void commitsOnlyAfterMatchingDurableAckAndRejectsCorruptAck() throws Exception {
        AtomicInteger accepted = new AtomicInteger(), flushed = new AtomicInteger(), calls = new AtomicInteger();
        var server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/api/cdc/streams/test/lease", exchange -> {
            exchange.getRequestBody().readAllBytes();
            byte[] bytes = ("{\"fencing_token\":1,\"generation\":\"" + GENERATION + "\",\"lease_seconds\":60}").getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(200, bytes.length); exchange.getResponseBody().write(bytes); exchange.close();
        });
        server.createContext("/api/cdc/streams/test/append", exchange -> {
            var body = EnvelopeCodec.JSON.readTree(exchange.getRequestBody());
            assertEquals(0, accepted.get()); assertEquals(0, flushed.get());
            var ack = EnvelopeCodec.JSON.createObjectNode().put("received_sequence", 1);
            ack.putArray("accepted_event_ids").add(calls.getAndIncrement() == 0
                    ? "wrong-id" : body.path("events").get(0).path("event_id").asText());
            byte[] bytes = ack.toString().getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(200, bytes.length); exchange.getResponseBody().write(bytes); exchange.close();
        });
        server.start();
        try {
            var sink = new DurableHttpConsumer(URI.create("http://127.0.0.1:" + server.getAddress().getPort() + "/api/cdc/streams/test"),
                    "test-token", GENERATION, Set.of("127.0.0.1"), true, 10, 8192);
            RecordChangeEvent<SourceRecord> event = () -> new SourceRecord(Map.of("server", "test"), Map.of("lsn", 1L), "test.items", Schema.INT64_SCHEMA, 1L, null, null);
            var committer = new DebeziumEngine.RecordCommitter<RecordChangeEvent<SourceRecord>>() {
                @Override public void markProcessed(RecordChangeEvent<SourceRecord> record) { accepted.incrementAndGet(); }
                @Override public void markBatchFinished() { flushed.incrementAndGet(); }
                @Override public void markProcessed(RecordChangeEvent<SourceRecord> record, DebeziumEngine.Offsets offsets) { fail("Custom offset override forbidden"); }
                @Override public DebeziumEngine.Offsets buildOffsets() { throw new UnsupportedOperationException(); }
            };
            assertThrows(IllegalStateException.class, () -> sink.handleBatch(List.of(event), committer));
            assertEquals(0, accepted.get()); assertEquals(0, flushed.get());
            sink.handleBatch(List.of(event), committer);
            assertEquals(1, accepted.get()); assertEquals(1, flushed.get());
        } finally { server.stop(0); }
    }

    @Test void requiresExplicitHostAndPlainHttpException() {
        assertThrows(IllegalArgumentException.class, () -> new DurableHttpConsumer(URI.create("http://127.0.0.1/api/cdc/streams/test"),
                "test", GENERATION, Set.of("127.0.0.1"), false, 10, 8192));
        assertThrows(IllegalArgumentException.class, () -> new DurableHttpConsumer(URI.create("https://evil.example/api/cdc/streams/test"),
                "test", GENERATION, Set.of("approved.example"), false, 10, 8192));
    }
}
