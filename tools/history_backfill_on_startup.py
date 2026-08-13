def onStartup():
    """Resume a controlled SQLite-to-Ignition historian backfill asynchronously."""

    def runBackfill():
        import os
        import sys
        from java.io import BufferedReader, File, FileInputStream, InputStreamReader, RandomAccessFile
        from java.lang import Thread
        from java.util.zip import GZIPInputStream

        logger = system.util.getLogger("gizmo.history.backfill")
        root = "/var/lib/gizmo-ignition/history-import"
        manifestPath = os.path.join(root, "staged", "manifest.json")
        controlPath = os.path.join(root, "control.json")
        serviceUnits = [
            ("gizmo_target", "gizmo.target"),
            ("gizmo_network_service", "gizmo-network.service"),
            ("gizmo_hardware_service", "gizmo-hardware.service"),
            ("gizmo_control_socket", "gizmo-control.socket"),
            ("gizmo_control_service", "gizmo-control.service"),
            ("gizmo_zmon_service", "gizmo-zmon.service"),
            ("gizmo_display_service", "gizmo-display.service"),
            ("gizmo_temperature_service", "gizmo-temperature.service"),
            ("gizmo_sdr_service", "gizmo-sdr.service"),
            ("gizmo_zmq_service", "gizmo-zmq.service"),
            ("gizmo_opcua_service", "gizmo-opcua.service"),
            ("gizmo_historian_service", "gizmo-historian.service"),
            ("gizmo_dashboard_service", "gizmo-dashboard.service"),
        ]

        def readJson(path):
            stream = open(path, "r")
            try:
                return system.util.jsonDecode(stream.read())
            finally:
                stream.close()

        def writeJsonAtomic(path, value):
            temporary = path + ".tmp"
            stream = open(temporary, "w")
            try:
                stream.write(system.util.jsonEncode(value, 2))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            finally:
                stream.close()
            os.rename(temporary, path)

        def utcNow():
            return system.date.format(system.date.now(), "yyyy-MM-dd'T'HH:mm:ss.SSSXXX")

        def safeBasename(value, field, default):
            name = str(value or default)
            if name in (".", "..") or os.path.basename(name) != name:
                raise ValueError(field + " must be a plain file name")
            return name

        def ensureServiceTagPaths(control):
            """Create parser-safe aliases for legacy dotted service folders."""

            if not bool(control.get("migrate_service_tag_paths", False)):
                return
            resultPath = os.path.join(root, "service-tag-migration-result.json")
            basePath = "[default]GIZMo/Services/Units"
            expectedPaths = [
                basePath + "/" + unitKey + "/RestartCount"
                for unitKey, displayUnit in serviceUnits
            ]
            if all(system.tag.exists(path) for path in expectedPaths):
                writeJsonAtomic(
                    resultPath,
                    {
                        "status": "already-present",
                        "base_path": basePath,
                        "restart_count_paths": expectedPaths,
                        "updated_at": utcNow(),
                    },
                )
                return

            resourceRoot = str(
                control.get(
                    "legacy_service_tag_resource_root",
                    "/opt/ignition/data/config/resources/"
                    "core/ignition/tag-definition/default/GIZMo/Services/Units",
                )
            )
            folders = []
            sources = []
            for unitKey, displayUnit in serviceUnits:
                tagsPath = os.path.join(resourceRoot, displayUnit, "tags.json")
                if not os.path.isfile(tagsPath):
                    raise ValueError("legacy service tag resource is missing: " + tagsPath)
                tags = list(readJson(tagsPath))
                folders.append(
                    {"name": unitKey, "tagType": "Folder", "tags": tags}
                )
                sources.append(tagsPath)

            configureResults = list(system.tag.configure(basePath, folders, "m"))
            configureFailures = [
                str(result) for result in configureResults if not result.isGood()
            ]
            Thread.sleep(500)
            missing = [
                path for path in expectedPaths if not system.tag.exists(path)
            ]
            result = {
                "status": (
                    "complete"
                    if not configureFailures and not missing
                    else "failed"
                ),
                "base_path": basePath,
                "source_resources": sources,
                "restart_count_paths": expectedPaths,
                "configure_results": [str(item) for item in configureResults],
                "configure_failures": configureFailures,
                "missing_paths": missing,
                "updated_at": utcNow(),
            }
            writeJsonAtomic(resultPath, result)
            if configureFailures or missing:
                raise RuntimeError(
                    "service tag migration failed: configure=%s missing=%s"
                    % (configureFailures, missing)
                )

        def liveTagPath(historyPath):
            marker = ":/tag:"
            if marker not in historyPath:
                raise ValueError("historical path has no tag component: " + historyPath)
            tagPath = historyPath.split(marker, 1)[1]
            # Staging produced before the parser-safe service migration used
            # human-readable dotted systemd names. Accept those manifests but
            # always resolve them to the canonical OPC-safe unit key.
            for unitKey, displayUnit in serviceUnits:
                legacy = "/Services/Units/" + displayUnit + "/"
                replacement = "/Services/Units/" + unitKey + "/"
                tagPath = tagPath.replace(legacy, replacement)
            return "[default]" + tagPath

        def qualifiedHistorianPath(historyPath):
            marker = ":/sys:"
            if not historyPath.startswith("histprov:") or marker not in historyPath:
                raise ValueError(
                    "historical path cannot be retargeted: " + historyPath
                )
            return (
                "histprov:"
                + destinationHistorian
                + marker
                + historyPath.split(marker, 1)[1]
            )

        def storagePath(historyPath):
            if storagePathMode == "live-tag":
                return liveTagPath(historyPath)
            return qualifiedHistorianPath(historyPath)

        def valuesEqual(actual, expected):
            if isinstance(expected, bool):
                return bool(actual) == expected
            if isinstance(expected, (int, long, float)):
                try:
                    difference = abs(float(actual) - float(expected))
                    scale = max(1.0, abs(float(expected)))
                    return difference <= 1.0e-9 * scale
                except (TypeError, ValueError):
                    return False
            return actual == expected

        def queryExact(historyPath, timestampMs, expectedValue, expectedQuality):
            rows = []
            lastError = None
            for queryAttempt in range(queryAttempts):
                try:
                    dataset = system.historian.queryRawPoints(
                        paths=[historyPath],
                        startTime=system.date.fromMillis(timestampMs - 1000),
                        endTime=system.date.fromMillis(timestampMs + 1000),
                        columnNames=["value"],
                        returnFormat="TALL",
                        returnSize=-1,
                        includeBounds=True,
                        excludeObservations=False,
                    )
                    rows = []
                    for rowIndex in range(dataset.getRowCount()):
                        row = {}
                        for columnIndex in range(dataset.getColumnCount()):
                            column = str(dataset.getColumnName(columnIndex))
                            itemValue = dataset.getValueAt(rowIndex, columnIndex)
                            if hasattr(itemValue, "getTime"):
                                itemValue = int(itemValue.getTime())
                            elif hasattr(itemValue, "getCode"):
                                itemValue = int(itemValue.getCode())
                            row[column] = itemValue
                        rows.append(row)
                    for row in rows:
                        if (
                            row.get("timestamp") == timestampMs
                            and valuesEqual(row.get("value"), expectedValue)
                            and (int(row.get("quality", -1)) & 0xFFFF)
                            == expectedQuality
                        ):
                            return {
                                "matched": True,
                                "rows": rows,
                                "error": None,
                            }
                except:
                    lastError = str(sys.exc_info()[1])
                if queryAttempt + 1 < queryAttempts:
                    Thread.sleep(queryDelayMs)
            return {"matched": False, "rows": rows, "error": lastError}

        if not os.path.isfile(manifestPath):
            logger.info("No staged GIZMo history manifest; backfill is idle")
            return
        if not os.path.isfile(controlPath):
            logger.warn("No GIZMo history control file; backfill is disabled")
            return

        try:
            control = readJson(controlPath)
            if not bool(control.get("enabled", False)):
                logger.info("GIZMo history backfill is disabled by control.json")
                return
            storagePathMode = str(
                control.get("storage_path_mode", "live-tag") or "live-tag"
            )
            if storagePathMode not in ("live-tag", "qualified-historian"):
                raise ValueError("unsupported historian storage path mode")
            destinationHistorian = str(
                control.get("destination_historian", "") or ""
            )
            if storagePathMode == "qualified-historian" and not destinationHistorian:
                raise ValueError(
                    "destination_historian is required for qualified-historian mode"
                )
            stateName = safeBasename(
                control.get("state_file"), "state_file", "state.json"
            )
            validationName = safeBasename(
                control.get("validation_file"),
                "validation_file",
                "validation-result.json",
            )
            probeResultName = safeBasename(
                control.get("probe_result_file"),
                "probe_result_file",
                "probe-result.json",
            )
            lockName = safeBasename(
                control.get("lock_file"), "lock_file", "backfill.lock"
            )
            queryAttempts = max(
                1, min(int(control.get("query_attempts", 3)), 600)
            )
            queryDelayMs = max(
                0, min(int(control.get("query_delay_ms", 250)), 10000)
            )
        except Exception as error:
            logger.error("Invalid GIZMo history control file: " + str(error))
            return

        statePath = os.path.join(root, stateName)
        validationPath = os.path.join(root, validationName)
        probeResultPath = os.path.join(root, probeResultName)
        lockPath = os.path.join(root, lockName)

        lockFile = RandomAccessFile(lockPath, "rw")
        lockChannel = lockFile.getChannel()
        lock = lockChannel.tryLock()
        if lock is None:
            lockChannel.close()
            lockFile.close()
            logger.info("Another GIZMo history backfill worker already owns the lock")
            return

        state = {
            "format_version": 1,
            "status": "idle",
            "files": {},
            "points_written": 0,
            "rows_written": 0,
            "null_points_skipped": 0,
            "storage_path_mode": storagePathMode,
            "destination_historian": destinationHistorian,
        }

        try:
            manifest = readJson(manifestPath)
            startupDelay = max(
                0, min(int(control.get("startup_delay_seconds", 15)), 120)
            )
            if startupDelay:
                logger.info(
                    "Waiting %d seconds for historian providers to start"
                    % startupDelay
                )
                Thread.sleep(startupDelay * 1000)
            if int(manifest.get("format_version", 0)) != 1:
                raise ValueError("unsupported GIZMo history manifest version")
            ensureServiceTagPaths(control)

            if os.path.isfile(statePath):
                existing = readJson(statePath)
                if int(existing.get("format_version", 0)) != 1:
                    raise ValueError("unsupported GIZMo history state version")
                if existing.get("source_sha256") not in (None, manifest["source"]["sha256"]):
                    raise ValueError("state belongs to a different SQLite source")
                if str(existing.get("storage_path_mode", "live-tag")) != storagePathMode:
                    raise ValueError("state belongs to a different historian path mode")
                if str(existing.get("destination_historian", "") or "") != destinationHistorian:
                    raise ValueError("state belongs to a different destination historian")
                state.update(existing)

            state["source_sha256"] = manifest["source"]["sha256"]
            state["storage_path_mode"] = storagePathMode
            state["destination_historian"] = destinationHistorian
            state["status"] = "running"
            state["started_at"] = utcNow()
            state["last_error"] = None
            writeJsonAtomic(statePath, state)

            batchLimit = max(100, min(int(control.get("batch_points", 5000)), 25000))
            maximumRows = max(0, int(control.get("max_rows_per_file", 0)))
            includeDays = set(str(day) for day in control.get("include_days", []))
            stopPath = os.path.join(root, "STOP")
            groups = manifest["groups"]
            files = manifest["files"]
            selectedFiles = []
            for item in files:
                if not includeDays or str(item["day"]) in includeDays:
                    selectedFiles.append(item)

            probeSource = str(control.get("probe_source_path", "") or "")
            if probeSource:
                probeHistory = str(control.get("probe_history_path", "") or "")
                matched = False
                for item in selectedFiles:
                    group = str(item["group"])
                    sourcePaths = list(groups[group]["source_paths"])
                    if probeSource not in sourcePaths:
                        continue
                    matched = True
                    index = sourcePaths.index(probeSource)
                    historyPath = probeHistory or list(
                        groups[group]["historical_paths"]
                    )[index]
                    storePath = storagePath(historyPath)
                    absolutePath = os.path.join(
                        root, "staged", str(item["path"])
                    )
                    reader = BufferedReader(
                        InputStreamReader(
                            GZIPInputStream(FileInputStream(absolutePath)),
                            "UTF-8",
                        )
                    )
                    try:
                        line = reader.readLine()
                    finally:
                        reader.close()
                    record = system.util.jsonDecode(line)
                    timestampMs = int(record[0])
                    value = list(record[1])[index]
                    quality = int(list(record[2])[index])
                    result = system.historian.storeDataPoints(
                        [storePath],
                        [value],
                        [system.date.fromMillis(timestampMs)],
                        [quality],
                        True,
                    )
                    if hasattr(result, "isGood"):
                        accepted = bool(result.isGood())
                        resultText = str(result)
                    else:
                        resultList = list(result)
                        accepted = all(item.isGood() for item in resultList)
                        resultText = ", ".join(str(item) for item in resultList)
                    readbackRows = []
                    readbackMatched = False
                    readbackError = None
                    if accepted:
                        query = queryExact(
                            storePath, timestampMs, value, quality
                        )
                        readbackRows = query["rows"]
                        readbackMatched = bool(query["matched"])
                        readbackError = query["error"]
                    probe = {
                        "source_path": probeSource,
                        "historical_path": historyPath,
                        "store_path": storePath,
                        "timestamp_ms": timestampMs,
                        "value": value,
                        "quality": quality,
                        "accepted": accepted,
                        "result": resultText,
                        "readback_matched": readbackMatched,
                        "readback_rows": readbackRows,
                        "readback_error": readbackError,
                        "updated_at": utcNow(),
                    }
                    writeJsonAtomic(probeResultPath, probe)
                    if accepted and readbackMatched:
                        state["status"] = "probe-complete"
                    elif accepted:
                        state["status"] = "probe-readback-failed"
                    else:
                        state["status"] = "probe-rejected"
                    state["updated_at"] = utcNow()
                    if not accepted:
                        state["last_error"] = resultText
                    elif not readbackMatched:
                        state["last_error"] = readbackError or "historian readback did not match"
                    else:
                        state["last_error"] = None
                    writeJsonAtomic(statePath, state)
                    logger.info(
                        "GIZMo history probe: accepted=%s path=%s result=%s"
                        % (accepted, historyPath, resultText)
                    )
                    return
                if not matched:
                    raise ValueError(
                        "probe source path is not present in selected files: "
                        + probeSource
                    )

            for item in selectedFiles:
                if os.path.isfile(stopPath):
                    state["status"] = "paused"
                    state["updated_at"] = utcNow()
                    writeJsonAtomic(statePath, state)
                    logger.warn("GIZMo history backfill paused by STOP file")
                    return

                group = str(item["group"])
                sourcePaths = list(groups[group]["source_paths"])
                historyPaths = list(groups[group]["historical_paths"])
                storePaths = [storagePath(path) for path in historyPaths]
                if len(sourcePaths) != len(historyPaths):
                    raise ValueError("manifest path arrays differ for group " + group)

                key = str(item["sha256"])
                fileState = state["files"].get(key, {})
                completedRows = int(fileState.get("rows", 0))
                totalRows = int(item["rows"])
                targetRows = totalRows
                if maximumRows:
                    targetRows = min(targetRows, maximumRows)
                if completedRows >= targetRows:
                    continue

                absolutePath = os.path.join(root, "staged", str(item["path"]))
                reader = BufferedReader(
                    InputStreamReader(
                        GZIPInputStream(FileInputStream(absolutePath)),
                        "UTF-8",
                    ),
                    1024 * 1024,
                )
                rowNumber = 0
                batchPaths = []
                batchValues = []
                batchTimestamps = []
                batchQualities = []
                batchEndRow = completedRows
                progress = {
                    "points": int(fileState.get("points", 0)),
                    "skipped": int(fileState.get("null_points_skipped", 0)),
                    "pending_skipped": 0,
                }

                def flushBatch():
                    if not batchPaths and batchEndRow <= completedRows:
                        return
                    written = len(batchPaths)
                    if batchPaths:
                        results = system.historian.storeDataPoints(
                            batchPaths,
                            batchValues,
                            batchTimestamps,
                            batchQualities,
                            True,
                        )
                        if hasattr(results, "isGood"):
                            failures = [] if results.isGood() else [str(results)]
                        else:
                            failures = [
                                str(result) for result in results if not result.isGood()
                            ]
                        if failures:
                            raise RuntimeError(
                                "historian rejected %d point(s): %s"
                                % (len(failures), ", ".join(failures[:5]))
                            )
                    progress["points"] += written
                    progress["skipped"] += progress["pending_skipped"]
                    progress["pending_skipped"] = 0
                    fileState["rows"] = batchEndRow
                    fileState["points"] = progress["points"]
                    fileState["null_points_skipped"] = progress["skipped"]
                    fileState["path"] = str(item["path"])
                    fileState["day"] = str(item["day"])
                    fileState["group"] = group
                    fileState["updated_at"] = utcNow()
                    state["files"][key] = fileState
                    state["rows_written"] = sum(
                        int(value.get("rows", 0)) for value in state["files"].values()
                    )
                    state["points_written"] = sum(
                        int(value.get("points", 0)) for value in state["files"].values()
                    )
                    state["null_points_skipped"] = sum(
                        int(value.get("null_points_skipped", 0))
                        for value in state["files"].values()
                    )
                    state["updated_at"] = utcNow()
                    writeJsonAtomic(statePath, state)
                    del batchPaths[:]
                    del batchValues[:]
                    del batchTimestamps[:]
                    del batchQualities[:]

                try:
                    while True:
                        line = reader.readLine()
                        if line is None:
                            break
                        rowNumber += 1
                        if rowNumber <= completedRows:
                            continue
                        if rowNumber > targetRows:
                            break
                        record = system.util.jsonDecode(line)
                        if not isinstance(record, list) or len(record) != 3:
                            raise ValueError("invalid staged history record")
                        timestampMs = int(record[0])
                        values = list(record[1])
                        qualities = [int(value) for value in record[2]]
                        if len(values) != len(historyPaths) or len(qualities) != len(historyPaths):
                            raise ValueError("staged record does not match manifest paths")
                        nonNullCount = sum(1 for value in values if value is not None)
                        if batchPaths and len(batchPaths) + nonNullCount > batchLimit:
                            flushBatch()
                            if os.path.isfile(stopPath):
                                state["status"] = "paused"
                                state["updated_at"] = utcNow()
                                writeJsonAtomic(statePath, state)
                                return
                        timestamp = system.date.fromMillis(timestampMs)
                        for pointIndex in range(len(historyPaths)):
                            if values[pointIndex] is None:
                                # Ignition has no value-less historical sample type.
                                # Preserve absence as a gap; companion range and
                                # status tags retain the reason (for example,
                                # ResistanceRange=OutOfRange for HIGH Z).
                                progress["pending_skipped"] += 1
                                continue
                            batchPaths.append(storePaths[pointIndex])
                            batchValues.append(values[pointIndex])
                            batchTimestamps.append(timestamp)
                            batchQualities.append(qualities[pointIndex])
                        batchEndRow = rowNumber
                    flushBatch()
                finally:
                    reader.close()

                if int(fileState.get("rows", 0)) >= totalRows:
                    fileState["complete"] = True
                    fileState["completed_at"] = utcNow()
                    state["files"][key] = fileState
                    writeJsonAtomic(statePath, state)

            validationChecks = []
            validationPassed = True
            for item in selectedFiles:
                group = str(item["group"])
                sourcePaths = list(groups[group]["source_paths"])
                historyPaths = list(groups[group]["historical_paths"])
                targetRows = int(item["rows"])
                if maximumRows:
                    targetRows = min(targetRows, maximumRows)
                key = str(item["sha256"])
                if int(state["files"].get(key, {}).get("rows", 0)) < targetRows:
                    continue

                absolutePath = os.path.join(root, "staged", str(item["path"]))
                reader = BufferedReader(
                    InputStreamReader(
                        GZIPInputStream(FileInputStream(absolutePath)), "UTF-8"
                    ),
                    1024 * 1024,
                )
                boundaryRecords = []
                validationRows = set(
                    [
                        1,
                        min(100, targetRows),
                        min(101, targetRows),
                        max(1, targetRows // 2),
                        targetRows,
                    ]
                )
                try:
                    for rowNumber in range(1, targetRows + 1):
                        line = reader.readLine()
                        if line is None:
                            raise ValueError("staged file ended during validation")
                        if rowNumber in validationRows:
                            boundaryRecords.append(
                                (rowNumber, system.util.jsonDecode(line))
                            )
                finally:
                    reader.close()

                for rowNumber, record in boundaryRecords:
                    timestampMs = int(record[0])
                    values = list(record[1])
                    qualities = [int(value) for value in record[2]]
                    pointIndex = None
                    for candidateIndex in range(len(values)):
                        if (
                            values[candidateIndex] is not None
                            and qualities[candidateIndex] == 203
                        ):
                            pointIndex = candidateIndex
                            break
                    for candidateIndex in range(len(values)):
                        if pointIndex is None and values[candidateIndex] is not None:
                            pointIndex = candidateIndex
                            break
                    if pointIndex is None:
                        raise ValueError("validation boundary has no stored values")
                    # Core Historian reads resolve through live tags to avoid
                    # an Ignition 8.3.8 qualified-path node-cache defect. SQL
                    # migrations deliberately target and query the explicit
                    # destination provider before live tags are cut over.
                    queryPath = storagePath(historyPaths[pointIndex])
                    query = queryExact(
                        queryPath,
                        timestampMs,
                        values[pointIndex],
                        qualities[pointIndex],
                    )
                    check = {
                        "day": str(item["day"]),
                        "group": group,
                        "row": rowNumber,
                        "source_path": sourcePaths[pointIndex],
                        "historical_path": historyPaths[pointIndex],
                        "query_path": queryPath,
                        "timestamp_ms": timestampMs,
                        "expected_value": values[pointIndex],
                        "expected_quality": qualities[pointIndex],
                        "matched": bool(query["matched"]),
                        "query_rows": query["rows"],
                        "query_error": query["error"],
                    }
                    validationChecks.append(check)
                    if not check["matched"]:
                        validationPassed = False

            validation = {
                "source_sha256": manifest["source"]["sha256"],
                "checks": validationChecks,
                "passed": validationPassed and bool(validationChecks),
                "updated_at": utcNow(),
            }
            validation["storage_path_mode"] = storagePathMode
            validation["destination_historian"] = destinationHistorian
            writeJsonAtomic(validationPath, validation)
            state["validation"] = {
                "passed": validation["passed"],
                "checks": len(validationChecks),
                "updated_at": validation["updated_at"],
            }
            if not validation["passed"]:
                state["status"] = "validation-failed"
                state["updated_at"] = utcNow()
                state["last_error"] = "historian boundary readback did not match"
                writeJsonAtomic(statePath, state)
                logger.error("GIZMo history boundary validation failed")
                return

            fullySelected = not includeDays and maximumRows == 0
            allComplete = fullySelected and all(
                int(state["files"].get(str(item["sha256"]), {}).get("rows", 0))
                >= int(item["rows"])
                for item in files
            )
            state["status"] = "complete" if allComplete else "pilot-complete"
            state["updated_at"] = utcNow()
            state["completed_at"] = utcNow()
            writeJsonAtomic(statePath, state)
            logger.info(
                "GIZMo history backfill run finished: status=%s rows=%s points=%s"
                % (state["status"], state["rows_written"], state["points_written"])
            )
        except Exception as error:
            state["status"] = "failed"
            state["updated_at"] = utcNow()
            state["last_error"] = str(error)
            try:
                writeJsonAtomic(statePath, state)
            except Exception:
                pass
            logger.error("GIZMo history backfill failed: " + str(error))
        finally:
            lock.release()
            lockChannel.close()
            lockFile.close()

    system.util.invokeAsynchronous(runBackfill, "GIZMo-History-Backfill")
