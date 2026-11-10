package local.ontology.cdc;

import org.junit.jupiter.api.Test;
import java.util.Properties;
import static org.junit.jupiter.api.Assertions.*;

final class MainTest {
    @Test void fullTableBaselineRejectsUnreviewedSnapshotFilters() {
        assertDoesNotThrow(() -> Main.validateSnapshotScope(new Properties()));
        for (String key : new String[]{"snapshot.select.statement.overrides", "snapshot.select.statement.overrides.public.items",
                "snapshot.include.collection.list", "snapshot.query.mode.custom.name", "snapshot.query.mode"}) {
            var properties = new Properties();
            properties.setProperty(key, "custom");
            assertThrows(IllegalArgumentException.class, () -> Main.validateSnapshotScope(properties));
        }
    }
}
