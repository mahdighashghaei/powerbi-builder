Feature: Build a PBIP project from a CSV
  As a business user, I want to provide a CSV file and a plain-English
  description so that the multi-agent system produces a valid Power BI Project
  (.pbip) folder I can open in Power BI Desktop.

  Background:
    Given a CSV file with sales data

  Scenario: A valid CSV and description produce a valid PBIP structure
    When the builder runs with the description "Monthly sales by region"
    Then the .pbip folder should exist
    And the .SemanticModel folder should contain a TMDL table definition
    And the .Report folder should contain at least one page
    And the validation should pass

  Scenario: A missing input file is rejected without crashing
    When the builder runs with a non-existent source file
    Then the run should fail with a clear error
    And no output folder should be created

  Scenario: The build writes a versioned specification artifact
    When the builder runs with the description "sales by region"
    Then a build.spec.json should be written next to the README
    And the spec should record the schema version and project name
