package local.ontology.cdc;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import io.debezium.engine.DebeziumEngine;
import io.debezium.engine.RecordChangeEvent;
import org.apache.kafka.connect.source.SourceRecord;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.UUID;

/** Serial at-least-once delivery. Never acknowledge an uncommitted receiver batch. */
public final class DurableHttpConsumer implements DebeziumEngine.ChangeConsumer<RecordChangeEvent<SourceRecord>> {
    private final HttpClient client;
    private final URI stream;
    private final String token, generation, holder;
    private final EnvelopeCodec codec;
    private final int maxEvents, maxBytes;

    public DurableHttpConsumer(URI stream, String token, String generation, Set<String> allowedHosts,
                               boolean allowHttp, int maxEvents, int maxBytes) {
        if (stream.getUserInfo() != null || stream.getFragment() != null || stream.getQuery() != null
                || !allowedHosts.contains(stream.getHost())
                || !(stream.getScheme().equals("https") || allowHttp && stream.getScheme().equals("http"))) {
            throw new IllegalArgumentException("Receiver URL not allowed");
        }
        if (!stream.getPath().matches("/api/cdc/streams/[a-zA-Z0-9-]+")) {
            throw new IllegalArgumentException("Receiver stream path invalid");
        }
        if (maxEvents < 1 || maxEvents > 1000 || maxBytes < 1024 || maxBytes > 8 * 1024 * 1024) {
            throw new IllegalArgumentException("Batch limit invalid");
        }
        this.stream = stream; this.token = token; this.generation = generation;
        this.maxEvents = maxEvents; this.maxBytes = maxBytes;
        this.holder = UUID.randomUUID().toString(); // fencing holder, never used as event identity
        this.codec = new EnvelopeCodec(generation);
        this.client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5))
                .followRedirects(HttpClient.Redirect.NEVER).build();
    }

    @Override public boolean supportsTombstoneEvents() { return true; }

    @Override public synchronized void handleBatch(List<RecordChangeEvent<SourceRecord>> records,
                         DebeziumEngine.RecordCommitter<RecordChangeEvent<SourceRecord>> committer) throws InterruptedException {
        List<ObjectNode> chunk = new ArrayList<>();
        int size = 1024; // reserve request metadata/fence overhead
        for (RecordChangeEvent<SourceRecord> record : records) {
            ObjectNode event = codec.encode(record.record());
            int bytes = event.toString().getBytes(StandardCharsets.UTF_8).length + 1;
            if (bytes + 1024 > maxBytes) throw new IllegalArgumentException("Single event exceeds receiver byte limit");
            if (!chunk.isEmpty() && (chunk.size() >= maxEvents || size + bytes > maxBytes)) {
                deliver(chunk);
                chunk.clear(); size = 1024;
            }
            chunk.add(event); size += bytes;
        }
        if (!chunk.isEmpty()) {
            deliver(chunk);
        }
        // Finish the original engine batch exactly once. A partial HTTP prefix is
        // already durable but may replay after a later chunk fails; the inbox dedups it.
        for (RecordChangeEvent<SourceRecord> accepted : records) committer.markProcessed(accepted);
        committer.markBatchFinished();
    }

    private void deliver(List<ObjectNode> events) throws InterruptedException {
        int attempts = 0;
        while (!Thread.currentThread().isInterrupted()) {
            try {
                ObjectNode leaseBody = EnvelopeCodec.JSON.createObjectNode().put("holder_id", holder);
                JsonNode lease = request("/lease", leaseBody);
                if (!generation.equals(lease.path("generation").asText()) || !lease.path("fencing_token").canConvertToLong()
                        || lease.path("lease_seconds").asInt() < 30) {
                    throw new IllegalStateException("Lease response mismatch");
                }
                ObjectNode body = EnvelopeCodec.JSON.createObjectNode();
                body.put("holder_id", holder); body.set("fencing_token", lease.get("fencing_token"));
                body.put("generation", generation);
                ArrayNode array = body.putArray("events"); events.forEach(array::add);
                JsonNode ack = request("/append", body);
                JsonNode ids = ack.path("accepted_event_ids");
                if (!ids.isArray() || ids.size() != events.size() || !ack.path("received_sequence").canConvertToLong()) {
                    throw new IllegalStateException("Receiver ACK mismatch");
                }
                for (int i = 0; i < events.size(); i++) {
                    if (!ids.get(i).asText().equals(events.get(i).path("event_id").asText())) {
                        throw new IllegalStateException("Receiver ACK mismatch");
                    }
                }
                return;
            } catch (IOException e) {
                // Retry unchanged records after ambiguous response, with a renewed fencing lease.
                Thread.sleep(Math.min(5000, 250L << Math.min(attempts++, 4)));
            }
        }
        throw new InterruptedException("Delivery interrupted");
    }

    private JsonNode request(String suffix, ObjectNode body) throws IOException, InterruptedException {
        HttpRequest request = HttpRequest.newBuilder(URI.create(stream + suffix))
                .timeout(Duration.ofSeconds(10)).header("Authorization", "Bearer " + token)
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofByteArray(body.toString().getBytes(StandardCharsets.UTF_8))).build();
        HttpResponse<byte[]> response = client.send(request, info -> new LimitedBody(256 * 1024));
        int status = response.statusCode();
        if (status == 429 || status >= 500) throw new IOException("Transient receiver failure");
        if (status != 200) throw new IllegalStateException("Receiver rejected delivery: HTTP " + status);
        return EnvelopeCodec.JSON.readTree(response.body());
    }

    /** Limit while receiving, rather than after an unbounded response allocation. */
    private static final class LimitedBody implements HttpResponse.BodySubscriber<byte[]> {
        private final int maximum;
        private final java.io.ByteArrayOutputStream output = new java.io.ByteArrayOutputStream();
        private final java.util.concurrent.CompletableFuture<byte[]> result = new java.util.concurrent.CompletableFuture<>();
        private java.util.concurrent.Flow.Subscription subscription;
        LimitedBody(int maximum) { this.maximum = maximum; }
        @Override public java.util.concurrent.CompletionStage<byte[]> getBody() { return result; }
        @Override public void onSubscribe(java.util.concurrent.Flow.Subscription subscription) {
            this.subscription = subscription; subscription.request(1);
        }
        @Override public void onNext(List<java.nio.ByteBuffer> buffers) {
            for (var buffer : buffers) {
                if (buffer.remaining() > maximum - output.size()) {
                    subscription.cancel();
                    result.completeExceptionally(new IOException("Receiver response exceeds limit"));
                    return;
                }
                byte[] bytes = new byte[buffer.remaining()]; buffer.get(bytes); output.writeBytes(bytes);
            }
            subscription.request(1);
        }
        @Override public void onError(Throwable error) { result.completeExceptionally(error); }
        @Override public void onComplete() { result.complete(output.toByteArray()); }
    }
}
