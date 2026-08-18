def handleTimerEvent():
    """Store non-Good fast telemetry without changing its quality."""

    from java.lang import Long
    from java.math import BigInteger

    logger = system.util.getLogger("gizmo.history.non_good_bridge")
    fastPaths = [
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
    platformPaths = [
        "[default]GIZMo/OperatingSystem/CpuUtilizationPercent",
        "[default]GIZMo/OperatingSystem/Load1Minute",
        "[default]GIZMo/OperatingSystem/Load5Minute",
        "[default]GIZMo/OperatingSystem/Load15Minute",
        "[default]GIZMo/OperatingSystem/MemoryUsedBytes",
        "[default]GIZMo/OperatingSystem/MemoryAvailableBytes",
        "[default]GIZMo/OperatingSystem/ProcessCount",
        "[default]GIZMo/OperatingSystem/OpenFileHandles",
        "[default]GIZMo/Storage/Filesystems/Root/UsedPercent",
        "[default]GIZMo/Storage/Filesystems/State/UsedPercent",
        "[default]GIZMo/Storage/Filesystems/Run/UsedPercent",
        "[default]GIZMo/Network/Interfaces/eth0/RxBytes",
        "[default]GIZMo/Network/Interfaces/eth0/TxBytes",
        "[default]GIZMo/Network/Interfaces/eth0/RxErrors",
        "[default]GIZMo/Network/Interfaces/eth0/TxErrors",
        "[default]GIZMo/Network/Interfaces/eth1/RxBytes",
        "[default]GIZMo/Network/Interfaces/eth1/TxBytes",
        "[default]GIZMo/Network/Interfaces/eth1/RxErrors",
        "[default]GIZMo/Network/Interfaces/eth1/TxErrors",
        "[default]GIZMo/Services/Units/gizmo_target/RestartCount",
        "[default]GIZMo/Services/Units/gizmo_network_service/RestartCount",
        "[default]GIZMo/Services/Units/gizmo_hardware_service/RestartCount",
        "[default]GIZMo/Services/Units/gizmo_control_socket/RestartCount",
        "[default]GIZMo/Services/Units/gizmo_control_service/RestartCount",
        "[default]GIZMo/Services/Units/gizmo_zmon_service/RestartCount",
        "[default]GIZMo/Services/Units/gizmo_display_service/RestartCount",
        "[default]GIZMo/Services/Units/gizmo_temperature_service/RestartCount",
        "[default]GIZMo/Services/Units/gizmo_sdr_service/RestartCount",
        "[default]GIZMo/Services/Units/gizmo_zmq_service/RestartCount",
        "[default]GIZMo/Services/Units/gizmo_opcua_service/RestartCount",
        "[default]GIZMo/Services/Units/gizmo_historian_service/RestartCount",
        "[default]GIZMo/Services/Units/gizmo_dashboard_service/RestartCount",
    ]
    now = system.date.now()
    globalsMap = system.util.getGlobals()
    platformBucketKey = "gizmo_non_good_history_platform_bucket"
    platformBucket = int(now.getTime() // 10000)
    includePlatform = int(globalsMap.get(platformBucketKey, -1)) != platformBucket
    livePaths = list(fastPaths)
    if includePlatform:
        livePaths.extend(platformPaths)
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
        key = "gizmo_non_good_history_last_error_ms"
        lastMs = int(globalsMap.get(key, 0))
        if nowMs - lastMs >= 60000:
            logger.error(message)
            globalsMap[key] = nowMs

    def historianValue(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, BigInteger):
            return Long(str(value))
        if isinstance(value, (int, long)):
            return Long(str(value))
        return value

    try:
        qualifiedValues = list(system.tag.readBlocking(livePaths))
        if includePlatform:
            globalsMap[platformBucketKey] = platformBucket
        timestamp = now
        points = []
        for index, qualifiedValue in enumerate(qualifiedValues):
            value = qualifiedValue.getValue()
            quality = qualifiedValue.getQuality()
            if value is None or quality.isGood():
                continue
            qualityCode = int(quality.getCode()) & 1023
            points.append(
                system.historian.types.dataPoint(
                    historicalPaths[index],
                    historianValue(value),
                    timestamp,
                    qualityCode,
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
    except:
        import sys

        logFailure("Non-Good history bridge failed: " + str(sys.exc_info()[1]))
