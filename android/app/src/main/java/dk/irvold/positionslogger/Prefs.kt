package dk.irvold.positionslogger

import android.content.Context
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.runBlocking

data class Settings(
    val serverUrl: String = "",
    val deviceId: String = "",
    val token: String = "",
    val intervalSec: Int = 30,
    val minDistanceM: Int = 0,
    /** Kassér fixes hvis den målte unøjagtighed er større end dette (meter). 0 = ingen grænse. */
    val maxAccuracyM: Int = 25,
    val highAccuracy: Boolean = true,
    val startOnBoot: Boolean = false,
    /** Brugeren har trykket "Start" - bruges til at genstarte service efter reboot/crash. */
    val wantRunning: Boolean = false,
) {
    /** Basis-URL uden afsluttende skråstreg, eller null hvis serverUrl er tom. */
    fun baseUrl(): String? {
        val base = serverUrl.trim().trimEnd('/')
        return if (base.isEmpty()) null else base
    }

    /** Fuld URL til ingest-endpointet, eller null hvis serverUrl er tom. */
    fun ingestUrl(): String? = baseUrl()?.let { "$it/api/positions" }

    /** Fuld URL til health-endpointet (bruges til at teste forbindelsen). */
    fun healthUrl(): String? = baseUrl()?.let { "$it/api/health" }

    fun isComplete(): Boolean =
        ingestUrl() != null && deviceId.isNotBlank() && token.isNotBlank()
}

val Context.dataStore by preferencesDataStore(name = "settings")

object Prefs {
    private val SERVER = stringPreferencesKey("server_url")
    private val DEVICE = stringPreferencesKey("device_id")
    private val TOKEN = stringPreferencesKey("token")
    private val INTERVAL = intPreferencesKey("interval_sec")
    private val MIN_DIST = intPreferencesKey("min_distance_m")
    private val MAX_ACC = intPreferencesKey("max_accuracy_m")
    private val HIGH_ACC = booleanPreferencesKey("high_accuracy")
    private val ON_BOOT = booleanPreferencesKey("start_on_boot")
    private val RUNNING = booleanPreferencesKey("want_running")

    fun flow(context: Context): Flow<Settings> = context.dataStore.data.map { p ->
        Settings(
            serverUrl = p[SERVER] ?: "",
            deviceId = p[DEVICE] ?: android.os.Build.MODEL.orEmpty().replace(" ", "-").lowercase(),
            token = p[TOKEN] ?: "",
            intervalSec = p[INTERVAL] ?: 30,
            minDistanceM = p[MIN_DIST] ?: 0,
            maxAccuracyM = p[MAX_ACC] ?: 25,
            highAccuracy = p[HIGH_ACC] ?: true,
            startOnBoot = p[ON_BOOT] ?: false,
            wantRunning = p[RUNNING] ?: false,
        )
    }

    fun read(context: Context): Settings = runBlocking { flow(context).first() }

    suspend fun save(context: Context, s: Settings) {
        context.dataStore.edit { p ->
            p[SERVER] = s.serverUrl
            p[DEVICE] = s.deviceId
            p[TOKEN] = s.token
            p[INTERVAL] = s.intervalSec
            p[MIN_DIST] = s.minDistanceM
            p[MAX_ACC] = s.maxAccuracyM
            p[HIGH_ACC] = s.highAccuracy
            p[ON_BOOT] = s.startOnBoot
        }
    }

    suspend fun setRunning(context: Context, running: Boolean) {
        context.dataStore.edit { it[RUNNING] = running }
    }
}
