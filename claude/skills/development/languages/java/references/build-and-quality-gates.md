# Java — Build Wiring for the Quality Gates

Complete, working wiring for the gates the J-standards in `../SKILL.md` require: NullAway as
a compile ERROR (J7, J18), one location for versions (J17), architecture rules as tests
(J22), mutation testing (J23), real integration tests (J24), and supply-chain integrity
(J19). Every version literal below was current as of Aug 2026 — re-verify before pinning,
then record the result in the catalog and nowhere else.

## 1. Gradle — version catalog + Error Prone + NullAway

`gradle/libs.versions.toml` — the ONLY place a version literal may appear (J17):

```toml
[versions]
java = "25"
errorprone = "2.41.0"        # NullAway requires Error Prone >= 2.36
errorpronePlugin = "4.2.0"
nullaway = "0.12.10"
jspecify = "1.0.0"
junit = "6.0.1"              # one version across Platform/Jupiter/Vintage
jqwik = "1.10.1"
archunit = "1.4.1"
pitest = "1.16.1"
pitestPlugin = "1.15.0"
testcontainers = "1.21.3"    # Spring Boot's BOM manages this when present
cyclonedxPlugin = "2.3.0"

[libraries]
jspecify = { module = "org.jspecify:jspecify", version.ref = "jspecify" }
errorprone-core = { module = "com.google.errorprone:error_prone_core", version.ref = "errorprone" }
nullaway = { module = "com.uber.nullaway:nullaway", version.ref = "nullaway" }
junit-bom = { module = "org.junit:junit-bom", version.ref = "junit" }
junit-jupiter = { module = "org.junit.jupiter:junit-jupiter" }
junit-platform-launcher = { module = "org.junit.platform:junit-platform-launcher" }
jqwik = { module = "net.jqwik:jqwik", version.ref = "jqwik" }
archunit = { module = "com.tngtech.archunit:archunit", version.ref = "archunit" }
testcontainers-junit = { module = "org.testcontainers:junit-jupiter", version.ref = "testcontainers" }
testcontainers-postgresql = { module = "org.testcontainers:postgresql", version.ref = "testcontainers" }

[plugins]
errorprone = { id = "net.ltgt.errorprone", version.ref = "errorpronePlugin" }
pitest = { id = "info.solidsoft.pitest", version.ref = "pitestPlugin" }
cyclonedx = { id = "org.cyclonedx.bom", version.ref = "cyclonedxPlugin" }
```

`build.gradle.kts` — the same tasks run locally and in CI:

```kotlin
import net.ltgt.gradle.errorprone.errorprone

plugins {
    java
    alias(libs.plugins.errorprone)
    alias(libs.plugins.pitest)
    alias(libs.plugins.cyclonedx)
}

java { toolchain { languageVersion = JavaLanguageVersion.of(libs.versions.java.get().toInt()) } }

dependencies {
    implementation(libs.jspecify)
    errorprone(libs.errorprone.core)
    errorprone(libs.nullaway)
    testImplementation(platform(libs.junit.bom))
    testImplementation(libs.junit.jupiter)
    testRuntimeOnly(libs.junit.platform.launcher)
    testImplementation(libs.jqwik)
    testImplementation(libs.archunit)
    testImplementation(libs.testcontainers.junit)
    testImplementation(libs.testcontainers.postgresql)
}

tasks.withType<JavaCompile>().configureEach {
    options.release = libs.versions.java.get().toInt()
    options.compilerArgs.add("-Xlint:all")
    options.errorprone {
        error("NullAway")
        option("NullAway:OnlyNullMarked", "true")
    }
}

tasks.test { useJUnitPlatform() }
```

The `net.ltgt.errorprone` plugin supplies the `--add-exports`/`--add-opens` flags Error
Prone needs on modern JDKs; Maven spells them out below. `OnlyNullMarked` scopes NullAway
to `@NullMarked` packages, so adoption ratchets package by package with severity already
at ERROR.

## 2. Maven — Error Prone + NullAway

`pom.xml` fragments (versions in `<properties>` / BOM imports — never inline, J17):

```xml
<properties>
  <maven.compiler.release>25</maven.compiler.release>
  <errorprone.version>2.41.0</errorprone.version><!-- NullAway requires >= 2.36 -->
  <nullaway.version>0.12.10</nullaway.version>
</properties>

<plugin>
  <groupId>org.apache.maven.plugins</groupId>
  <artifactId>maven-compiler-plugin</artifactId>
  <version>3.14.0</version>
  <configuration>
    <compilerArgs>
      <arg>-Xlint:all</arg>
      <arg>-XDcompilePolicy=simple</arg>
      <arg>--should-stop=ifError=FLOW</arg>
      <arg>-Xplugin:ErrorProne -Xep:NullAway:ERROR -XepOpt:NullAway:OnlyNullMarked=true</arg>
    </compilerArgs>
    <annotationProcessorPaths>
      <path><groupId>com.google.errorprone</groupId><artifactId>error_prone_core</artifactId><version>${errorprone.version}</version></path>
      <path><groupId>com.uber.nullaway</groupId><artifactId>nullaway</artifactId><version>${nullaway.version}</version></path>
    </annotationProcessorPaths>
  </configuration>
</plugin>
```

`.mvn/jvm.config` — required, Error Prone reaches into `jdk.compiler` internals
(whitespace-separated, so line breaks are formatting only):

```text
--add-exports jdk.compiler/com.sun.tools.javac.api=ALL-UNNAMED --add-exports jdk.compiler/com.sun.tools.javac.file=ALL-UNNAMED
--add-exports jdk.compiler/com.sun.tools.javac.main=ALL-UNNAMED --add-exports jdk.compiler/com.sun.tools.javac.model=ALL-UNNAMED
--add-exports jdk.compiler/com.sun.tools.javac.parser=ALL-UNNAMED --add-exports jdk.compiler/com.sun.tools.javac.processing=ALL-UNNAMED
--add-exports jdk.compiler/com.sun.tools.javac.tree=ALL-UNNAMED --add-exports jdk.compiler/com.sun.tools.javac.util=ALL-UNNAMED
--add-opens jdk.compiler/com.sun.tools.javac.code=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.comment=ALL-UNNAMED
```

## 3. JSpecify — the `@NullMarked` package template

The dependency is `org.jspecify:jspecify:1.0.0` (in the catalog above; Maven declares the
same coordinates as a compile-scope `<dependency>`). One `package-info.java` per production
package (J6), created in the same commit as the package — an unannotated package is
invisible under `OnlyNullMarked`:

```java
@NullMarked
package com.example.orders;

import org.jspecify.annotations.NullMarked;
```

## 4. ArchUnit — starter rule class (the J22 set)

Plain `archunit` artifact, ordinary JUnit tests — no engine coupling, runs on any platform
version. Adjust the root package and layer names; keep the rule set. An approved boundary
decision lands here as a new rule in the same PR (J22).

```java
package com.example.architecture;

import com.tngtech.archunit.core.domain.JavaClasses;
import com.tngtech.archunit.core.importer.ClassFileImporter;
import com.tngtech.archunit.core.importer.ImportOption;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import static com.tngtech.archunit.lang.syntax.ArchRuleDefinition.noClasses;
import static com.tngtech.archunit.library.Architectures.layeredArchitecture;
import static com.tngtech.archunit.library.GeneralCodingRules.NO_CLASSES_SHOULD_USE_FIELD_INJECTION;
import static com.tngtech.archunit.library.dependencies.SlicesRuleDefinition.slices;

class ArchitectureTest {

    private static JavaClasses classes;

    @BeforeAll
    static void importProductionClasses() {
        classes = new ClassFileImporter()
                .withImportOption(ImportOption.Predefined.DO_NOT_INCLUDE_TESTS)
                .importPackages("com.example");
    }

    @Test
    void layers_depend_only_inward() {
        layeredArchitecture().consideringAllDependencies()
                .layer("api").definedBy("com.example.api..")
                .layer("domain").definedBy("com.example.domain..")
                .layer("infrastructure").definedBy("com.example.infrastructure..")
                .whereLayer("api").mayNotBeAccessedByAnyLayer()
                .whereLayer("domain").mayOnlyBeAccessedByLayers("api", "infrastructure")
                .whereLayer("infrastructure").mayNotBeAccessedByAnyLayer()
                .check(classes);
    }

    @Test
    void no_package_cycles() {
        slices().matching("com.example.(*)..").should().beFreeOfCycles().check(classes);
    }

    @Test
    void domain_imports_no_framework_classes() {
        noClasses().that().resideInAPackage("com.example.domain..")
                .should().dependOnClassesThat().resideInAnyPackage(
                        "org.springframework..", "jakarta.persistence..", "jakarta.servlet..")
                .check(classes);
    }

    @Test
    void no_field_injection() {
        NO_CLASSES_SHOULD_USE_FIELD_INJECTION.check(classes);
    }

    @Test
    void no_java_util_date() {
        noClasses().should().dependOnClassesThat().haveFullyQualifiedName("java.util.Date").check(classes);
    }
}
```

The field-injection rule matches annotations by name, so the test module needs no Spring
dependency; `DO_NOT_INCLUDE_TESTS` keeps `@Autowired` test fields out of scope.

## 5. PIT — mutation testing

Gradle (plugin applied in section 1) and Maven:

```kotlin
pitest {
    pitestVersion = libs.versions.pitest.get()
    threads = 4
    mutationThreshold = 75
    withHistory = true
}
```

```xml
<plugin>
  <groupId>org.pitest</groupId>
  <artifactId>pitest-maven</artifactId>
  <version>1.16.1</version>
  <configuration>
    <threads>4</threads>
    <mutationThreshold>75</mutationThreshold>
    <withHistory>true</withHistory>
  </configuration>
</plugin>
```

- PIT 1.16+ runs JUnit 6 tests directly — no separate junit5 plugin artifact.
- `withHistory` is the incremental analysis: PR CI re-analyzes only changed classes and
  their mutants, which is what makes the 75% threshold on new/changed code affordable per
  PR (J23). A weekly job runs from `clean`, discarding history to force a full analysis.
- First sprint advisory (omit the threshold), then blocking at 75. Never push toward
  100 — equivalent mutants make that a noise generator.

## 6. Testcontainers — integration test templates

Spring Boot with `@ServiceConnection` (J24): swap `PostgreSQLContainer` for any supported
container type (MySQL, Redis, Kafka, ...) and the wiring stays identical — it replaces
hand-wired `@DynamicPropertySource` plumbing. The `static` container serves every test in
the class.

```java
package com.example.orders;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.testcontainers.service.connection.ServiceConnection;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;

import static org.assertj.core.api.Assertions.assertThat;

@SpringBootTest
@Testcontainers
class OrderRepositoryIT {

    @Container
    @ServiceConnection
    static PostgreSQLContainer<?> postgres = new PostgreSQLContainer<>("postgres:17-alpine");

    @Autowired
    OrderRepository orders;

    @Test
    void should_find_persisted_order_when_queried_by_id() {
        var saved = orders.save(Order.pending(new OrderId("ord-1")));
        assertThat(orders.findById(saved.id())).isPresent();
    }
}
```

Non-Spring variant — plain JUnit, explicit lifecycle, no extensions:

```java
package com.example.orders;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import static org.assertj.core.api.Assertions.assertThat;

class OrderStoreIT {

    static PostgreSQLContainer<?> postgres = new PostgreSQLContainer<>("postgres:17-alpine");
    static JdbcOrderStore store;

    @BeforeAll
    static void startContainer() {
        postgres.start();
        store = new JdbcOrderStore(
                postgres.getJdbcUrl(), postgres.getUsername(), postgres.getPassword());
    }

    @AfterAll
    static void stopContainer() {
        postgres.stop();
    }

    @Test
    void should_find_persisted_order_when_queried_by_id() {
        var saved = store.save(Order.pending(new OrderId("ord-1")));
        assertThat(store.findById(saved.id())).isPresent();
    }
}
```

## 7. Supply chain — verification metadata, OSV-Scanner, SBOM

Bootstrap Gradle dependency verification (J19), commit the generated
`gradle/verification-metadata.xml`, and review its diffs like code:

```bash
./gradlew --write-verification-metadata sha256 help
```

OSV-Scanner in CI — GitHub Actions job fragment; run it on `pull_request` plus a weekly
`schedule`, and treat CVSS ≥ 7 findings as merge-blocking (J19):

```yaml
jobs:
  osv-scan:
    permissions:
      actions: read
      contents: read
      security-events: write
    uses: google/osv-scanner-action/.github/workflows/osv-scanner-reusable.yml@v2
    with:
      scan-args: |-
        --recursive
        ./
```

SBOM via CycloneDX — Gradle: the plugin from section 1, then `./gradlew cyclonedxBom`.
Maven: `mvn org.cyclonedx:cyclonedx-maven-plugin:makeAggregateBom`, with the plugin
version pinned in `<properties>` like every other version literal.

## Checklist

- [ ] Error Prone + NullAway run inside the normal compile task — same command locally and in CI
- [ ] NullAway severity is ERROR, scoped by `OnlyNullMarked`
- [ ] Every production package has a `@NullMarked` `package-info.java`
- [ ] Version literals only in the catalog (Gradle) or BOMs + `<properties>` (Maven)
- [ ] `ArchitectureTest` present per module and green
- [ ] PIT incremental on PR at threshold 75; weekly full run scheduled
- [ ] `verification-metadata.xml` (or lockfile) committed; OSV-Scanner green; SBOM published

## Anti-Patterns to Avoid

| Anti-Pattern | Problem | Solution |
|--------------|---------|----------|
| Gates behind CI-only flags | Local builds pass, CI fails; developers learn to ignore red | Same tasks and flags locally and in CI |
| NullAway as a warning | Warnings scroll past; the null contract silently erodes | `-Xep:NullAway:ERROR` or nothing |
| PIT full-repo on every commit | Minutes-to-hours per push; the gate gets disabled | Incremental on PR; full run weekly |
| Catalog present, versions duplicated in modules | Two answers to "which version runs" | The catalog is the only location (J17) |
