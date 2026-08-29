package dk.irvold.positionslogger

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.location.Location
import android.os.BatteryManager
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleService
import androidx.lifecycle.lifecycleScope
import com.google.android.gms.location.FusedLocationProviderClient
import com.google.android.gms.location.LocationCallback
import com.google.android.gms.location.LocationRequest
import com.google.android.gms.location.LocationResult
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import org.json.JSONObject
import java.time.Instant

class LocationService : LifecycleService() {

    private lateinit var fused: FusedLocationProviderClient
    private val queue by lazy { QueueDb.get(this) }
    private val flushLock = Mutex()

    @Volatile private var settings: Settings = Settings()
    @Volatile private var lastFixAt: Long = 0L
    @Volatile private var lastStatus: String = "starter…"
    @Volatile private var dropped: Int = 0

    private val callback = object : LocationCallback() {
        override fun onLocationResult(result: LocationResult) {
            val loc = result.lastLocation ?: return

            // Accuracy-gate: kassér upræcise fixes, så skrald aldrig gemmes/sendes.
            val maxAcc = settings.maxAccuracyM
            if (maxAcc > 0 && loc.hasAccuracy() && loc.accuracy > maxAcc) {
                dropped++
                lastStatus = "sprang fix over (±${loc.accuracy.toInt()} m) · droppet $dropped"
                updateNotification()
                return
            }

            lastFixAt = System.currentTimeMillis()
            queue.enqueue(toJson(loc).toString())
            updateNotification()
            flush()
        }
    }

    override fun onCreate() {
        super.onCreate()
        fused = LocationServices.getFusedLocationProviderClient(this)
        createChannel()
        settings = Prefs.read(this)
        // Hold en frisk kopi af indstillingerne uden at blokere på hovedtråden ved hvert fix.
        lifecycleScope.launch {
            Prefs.flow(this@LocationService).collectLatest { settings = it }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)

        if (intent?.action == ACTION_STOP) {
            lifecycleScope.launch { Prefs.setRunning(this@LocationService, false) }
            stopSelf()
            return START_NOT_STICKY
        }

        val hasPerm = hasLocationPermission()
        try {
            startForegroundCompat(hasPerm)
        } catch (e: Exception) {
            stopSelf()
            return START_NOT_STICKY
        }

        if (!hasPerm) {
            lastStatus = "mangler placeringstilladelse"
            updateNotification()
            stopSelf()
            return START_NOT_STICKY
        }

        settings = Prefs.read(this)
        requestUpdates(settings)
        UploadWorker.schedulePeriodic(this)
        flush()
        return START_STICKY
    }

    override fun onDestroy() {
        runCatching { fused.removeLocationUpdates(callback) }
        super.onDestroy()
    }

    override fun onBind(intent: Intent): IBinder? {
        super.onBind(intent)
        return null
    }

    // --------------------------------------------------------------- location
    private fun requestUpdates(s: Settings) {
        val priority =
            if (s.highAccuracy) Priority.PRIORITY_HIGH_ACCURACY
            else Priority.PRIORITY_BALANCED_POWER_ACCURACY
        val intervalMs = s.intervalSec.coerceAtLeast(1) * 1000L

        val request = LocationRequest.Builder(priority, intervalMs)
            .setMinUpdateIntervalMillis(intervalMs / 2)
            .setMinUpdateDistanceMeters(s.minDistanceM.toFloat())
            .setWaitForAccurateLocation(false)
            .build()

        try {
            fused.removeLocationUpdates(callback)
            fused.requestLocationUpdates(request, callback, mainLooper)
            lastStatus = "logger hvert ${s.intervalSec}. sek"
        } catch (e: SecurityException) {
            lastStatus = "tilladelse afvist"
        }
        updateNotification()
    }

    private fun toJson(loc: Location): JSONObject {
        return JSONObject().apply {
            put("device_id", settings.deviceId.ifBlank { Build.MODEL })
            put("lat", loc.latitude)
            put("lon", loc.longitude)
            if (loc.hasAccuracy()) put("acc", loc.accuracy.toDouble())
            if (loc.hasAltitude()) put("alt", loc.altitude)
            if (loc.hasSpeed()) put("spd", loc.speed.toDouble())
            if (loc.hasBearing()) put("brg", loc.bearing.toDouble())
            batteryPercent()?.let { put("bat", it) }
            put("ts", Instant.ofEpochMilli(if (loc.time > 0) loc.time else System.currentTimeMillis()).toString())
        }
    }

    private fun batteryPercent(): Int? {
        val bm = getSystemService(Context.BATTERY_SERVICE) as? BatteryManager ?: return null
        val p = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
        return if (p in 0..100) p else null
    }

    private fun flush() {
        lifecycleScope.launch {
            if (!flushLock.tryLock()) return@launch
            try {
                when (val r = Uploader.flush(this@LocationService)) {
                    is Uploader.Result.Ok ->
                        lastStatus = "sendt ${r.sent}, i kø ${r.remaining}"
                    is Uploader.Result.Failed -> {
                        val kø = queue.count()
                        lastStatus = when {
                            r.reason.contains("token") || r.reason.contains("URL") ->
                                "kø $kø · ${r.reason}"
                            !Net.hasNetwork(this@LocationService) ->
                                "kø $kø · ingen internetforbindelse"
                            !Net.isVpnActive(this@LocationService) ->
                                "kø $kø · Tailscale er slukket?"
                            else ->
                                "kø $kø · serveren svarer ikke"
                        }
                    }
                    Uploader.Result.Idle -> {
                        val n = queue.count()
                        if (n > 0) lastStatus = "i kø $n"
                    }
                }
            } finally {
                flushLock.unlock()
                updateNotification()
            }
        }
    }

    // ----------------------------------------------------------- notification
    private fun startForegroundCompat(hasPermission: Boolean) {
        val type = if (hasPermission && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q)
            ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION else 0
        ServiceCompat.startForeground(this, NOTIF_ID, buildNotification(), type)
    }

    private fun updateNotification() {
        (getSystemService(NOTIFICATION_SERVICE) as NotificationManager)
            .notify(NOTIF_ID, buildNotification())
    }

    private fun buildNotification(): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val stop = PendingIntent.getService(
            this, 1, Intent(this, LocationService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val age = if (lastFixAt == 0L) "ingen fix endnu"
        else "sidste fix ${(System.currentTimeMillis() - lastFixAt) / 1000}s siden"

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("Positionslogger kører")
            .setContentText("$lastStatus · $age")
            .setSmallIcon(android.R.drawable.ic_menu_mylocation)
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, "Stop", stop)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
    }

    private fun createChannel() {
        val ch = NotificationChannel(
            CHANNEL_ID, "Positionslogning", NotificationManager.IMPORTANCE_LOW
        ).apply { description = "Vises mens positionen logges i baggrunden" }
        (getSystemService(NOTIFICATION_SERVICE) as NotificationManager).createNotificationChannel(ch)
    }

    private fun hasLocationPermission(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED ||
            ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_COARSE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    companion object {
        private const val CHANNEL_ID = "logging"
        private const val NOTIF_ID = 42
        const val ACTION_STOP = "dk.irvold.positionslogger.STOP"

        fun start(context: Context) {
            ContextCompat.startForegroundService(
                context, Intent(context, LocationService::class.java)
            )
        }

        fun stop(context: Context) {
            context.startService(
                Intent(context, LocationService::class.java).setAction(ACTION_STOP)
            )
        }
    }
}
