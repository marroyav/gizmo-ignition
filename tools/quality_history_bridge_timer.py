def handleTimerEvent():
    """Store non-Good fast telemetry without changing its quality."""

    logger = system.util.getLogger("gizmo.history.non_good_bridge")
    livePaths = [
        "[default]GIZMo/Measurement/ResistanceOhm",
        "[default]GIZMo/Measurement/CapacitanceNanofarad",
        "[default]GIZMo/Measurement/ThresholdOhm",
        "[default]GIZMo/Measurement/StimulusFrequencyHertz",
        "[default]GIZMo/Measurement/MagnitudeCount",
        "[default]GIZMo/Measurement/PhaseAtanDegrees",
        "[default]GIZMo/Measurement/PhaseAtan2Degrees",
        "[default]GIZMo/Measurement/PhaseInterpolatedDegrees",
        "[default]GIZMo/Measurement/InPhaseCount",
        "[default]GIZMo/Measurement/QuadratureCount",
        "[default]GIZMo/Thermal/ChassisTemperatureCelsius",
        "[default]GIZMo/Thermal/Cpu1TemperatureCelsius",
        "[default]GIZMo/Thermal/Cpu2TemperatureCelsius",
        "[default]GIZMo/Thermal/Cpu3TemperatureCelsius",
        "[default]GIZMo/Time/UptimeSeconds",
        "[default]GIZMo/SDR/FrameSequence",
        "[default]GIZMo/Alarm/Active",
        "[default]GIZMo/Measurement/ResistanceRange",
    ]
    relativePaths = [path.split("GIZMo/", 1)[1] for path in livePaths]
    historianProvider = "__GIZMO_HISTORIAN_PROVIDER__"
    gatewayName = "__GIZMO_GATEWAY_NAME__"
    tagProvider = "__GIZMO_TAG_PROVIDER__"
    historicalPaths = [
        "histprov:%s:/drv:%s:%s:/tag:GIZMo/%s"
        % (historianProvider, gatewayName, tagProvider, path)
        for path in relativePaths
    ]

    def logFailure(message):
        nowMs = system.date.now().getTime()
        globalsMap = system.util.getGlobals()
        key = "gizmo_non_good_history_last_error_ms"
        lastMs = int(globalsMap.get(key, 0))
        if nowMs - lastMs >= 60000:
            logger.error(message)
            globalsMap[key] = nowMs

    try:
        qualifiedValues = list(system.tag.readBlocking(livePaths))
        timestamp = system.date.now()
        points = []
        for index, qualifiedValue in enumerate(qualifiedValues):
            value = qualifiedValue.getValue()
            quality = qualifiedValue.getQuality()
            if value is None or quality.isGood():
                continue
            qualityCode = int(quality.getCode()) & 1023
            points.append(
                system.historian.types.dataPoint(
                    historicalPaths[index], value, timestamp, qualityCode
                )
            )
        if not points:
            return
        results = system.historian.storeDataPoints(points)
        if hasattr(results, "isGood"):
            failures = [] if results.isGood() else [str(results)]
        else:
            failures = [str(result) for result in results if not result.isGood()]
        if failures:
            logFailure("Non-Good history store failed: " + "; ".join(failures))
    except Exception as error:
        logFailure("Non-Good history bridge failed: " + str(error))
