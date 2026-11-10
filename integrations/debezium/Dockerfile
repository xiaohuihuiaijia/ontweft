FROM maven:3.9.11-eclipse-temurin-21@sha256:6fdc855a6ed81d288ca7ca37ac6ff5e9308b612485c0801d70b25a858c83d237 AS build
WORKDIR /build
COPY pom.xml .
COPY src src
RUN --mount=type=cache,target=/root/.m2 mvn --batch-mode --quiet package \
    org.apache.maven.plugins:maven-dependency-plugin:3.8.1:copy-dependencies \
    -DincludeScope=runtime -DoutputDirectory=/build/target/runtime-dependencies -Dmdep.useRepositoryLayout=true \
    org.apache.maven.plugins:maven-dependency-plugin:3.8.1:tree \
    -Dscope=runtime -DoutputType=json -DoutputFile=/build/target/runtime-dependency-tree.json
# Initial reproducible test runtime: intentionally same pinned JDK image.
# A smaller audited JRE image is a deployment optimization, not a CDC guarantee.
FROM maven:3.9.11-eclipse-temurin-21@sha256:6fdc855a6ed81d288ca7ca37ac6ff5e9308b612485c0801d70b25a858c83d237
COPY --from=build /build/target/debezium-inbox-adapter-0.1.0.jar /app/adapter.jar
USER 10001:10001
ENTRYPOINT ["java", "-Xmx384m", "-jar", "/app/adapter.jar"]
