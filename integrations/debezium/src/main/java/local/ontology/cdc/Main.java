package local.ontology.cdc;

import io.debezium.embedded.Connect;
import io.debezium.engine.DebeziumEngine;
import io.debezium.engine.RecordChangeEvent;
import io.debezium.engine.format.ChangeEventFormat;
import org.apache.kafka.connect.source.SourceRecord;

import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Arrays;
import java.util.Properties;
import java.util.Set;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.stream.Collectors;

public final class Main {
    public static void main(String[] args) {
        try {
            if (args.length != 1) throw new IllegalArgumentException("Configuration file required");
            Properties properties = new Properties();
            try (var reader = Files.newBufferedReader(Path.of(args[0]), StandardCharsets.UTF_8)) { properties.load(reader); }
            String token = Files.readString(Path.of(required(properties, "inbox.token.file")), StandardCharsets.UTF_8).strip();
            URI endpoint = URI.create(required(properties, "inbox.stream.url"));
            String generation = required(properties, "inbox.generation");
            if (!java.util.UUID.fromString(generation).toString().equals(generation)) {
                throw new IllegalArgumentException("Generation must be canonical UUID");
            }
            Set<String> hosts = Arrays.stream(required(properties, "inbox.allowed.hosts").split(","))
                    .map(String::strip).collect(Collectors.toSet());
            DurableHttpConsumer sink = new DurableHttpConsumer(endpoint, token, generation, hosts,
                    Boolean.parseBoolean(properties.getProperty("inbox.allow.http", "false")),
                    Integer.parseInt(properties.getProperty("inbox.max.events", "1000")),
                    Integer.parseInt(properties.getProperty("inbox.max.bytes", "8388608")));
            properties.keySet().removeIf(key -> key.toString().startsWith("inbox."));
            Set<String> sourceHosts = Arrays.stream(required(properties, "source.allowed.hosts").split(","))
                    .map(String::strip).collect(Collectors.toSet());
            if (!sourceHosts.contains(required(properties, "database.hostname"))) {
                throw new IllegalArgumentException("Database host not allowed");
            }
            properties.remove("source.allowed.hosts");
            for (String key : new String[]{"name", "database.hostname", "database.port", "database.user", "database.password",
                    "database.dbname", "topic.prefix", "table.include.list", "slot.name", "publication.name", "offset.storage.file.filename"}) {
                required(properties, key);
            }
            if (properties.containsKey("transforms") || properties.containsKey("predicates")) {
                throw new IllegalArgumentException("Transforms cannot discard native evidence");
            }
            validateSnapshotScope(properties);
            properties.setProperty("connector.class", "io.debezium.connector.postgresql.PostgresConnector");
            properties.setProperty("plugin.name", "pgoutput");
            properties.setProperty("publication.autocreate.mode", "disabled");
            properties.setProperty("slot.drop.on.stop", "false");
            properties.setProperty("provide.transaction.metadata", "true");
            properties.setProperty("tombstones.on.delete", "true");
            properties.setProperty("decimal.handling.mode", "precise");
            properties.setProperty("binary.handling.mode", "bytes");
            properties.setProperty("heartbeat.interval.ms", "0");
            properties.setProperty("skipped.operations", "none");
            properties.setProperty("event.processing.failure.handling.mode", "fail");
            properties.setProperty("record.processing.order", "ORDERED");
            properties.setProperty("record.processing.threads", "1");
            properties.setProperty("offset.storage", "org.apache.kafka.connect.storage.FileOffsetBackingStore");
            properties.setProperty("offset.flush.interval.ms", "0");
            properties.setProperty("snapshot.mode", "initial");
            properties.setProperty("snapshot.query.mode", "select_all");
            properties.setProperty("snapshot.isolation.mode", "repeatable_read");
            properties.setProperty("snapshot.max.threads", "1");
            // Upstream notifications share the SourceRecord queue and its durable ACK path.
            properties.setProperty("notification.enabled.channels", "sink");
            properties.setProperty("notification.sink.topic.name", properties.getProperty("topic.prefix") + ".__ontology_snapshot_notifications");
            AtomicBoolean failed = new AtomicBoolean(false);
            try (DebeziumEngine<RecordChangeEvent<SourceRecord>> engine = DebeziumEngine.create(ChangeEventFormat.of(Connect.class))
                    .using(properties).notifying(sink).using((success, message, error) -> {
                        if (!success) { failed.set(true); System.err.println("CDC_ENGINE_FAILED: inspect source status and receiver diagnostics"); }
                    }).build()) {
                Runtime.getRuntime().addShutdownHook(new Thread(() -> {
                    try { engine.close(); } catch (Exception ignored) { /* Do not log credentials from upstream errors. */ }
                }));
                engine.run();
            }
            if (failed.get()) System.exit(2);
        } catch (Exception e) {
            // Connector config and driver exceptions may contain credentials or row contents.
            System.err.println("CDC_ADAPTER_FAILED: " + e.getClass().getSimpleName());
            System.exit(2);
        }
    }

    private static String required(Properties properties, String key) {
        String value = properties.getProperty(key);
        if (value == null || value.isBlank()) throw new IllegalArgumentException("Required configuration missing: " + key);
        return value;
    }

    static void validateSnapshotScope(Properties properties) {
        for (String key : properties.stringPropertyNames()) {
            if (key.startsWith("snapshot.select.statement.overrides") || key.equals("snapshot.include.collection.list")
                    || key.equals("snapshot.query.mode.custom.name")) {
                throw new IllegalArgumentException("Partial/custom snapshot queries cannot prove this full-table baseline");
            }
        }
        if (!"select_all".equals(properties.getProperty("snapshot.query.mode", "select_all"))) {
            throw new IllegalArgumentException("Only the native full-table snapshot query is supported");
        }
    }
}
