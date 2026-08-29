package dk.irvold.positionslogger

import android.content.Context
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/** Sender køede positioner til serveren. Kaldes både fra service og fra WorkManager. */
object Uploader {
    private const val TAG = "Uploader"
    private const val BATCH = 200
    private val JSON = "application/json; charset=utf-8".toMediaType()

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .writeTimeout(20, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    sealed interface Result {
        data class Ok(val sent: Int, val remaining: Int) : Result
        data object Idle : Result
        data class Failed(val reason: String) : Result
    }

    /** Tømmer køen så vidt muligt. Kald fra én coroutine ad gangen. */
    suspend fun flush(context: Context): Result = withContext(Dispatchers.IO) {
        val s = Prefs.read(context)
        val url = s.ingestUrl() ?: return@withContext Result.Failed("server-URL mangler")
        if (s.token.isBlank()) return@withContext Result.Failed("token mangler")

        val db = QueueDb.get(context)
        db.trimTo(QueueDb.MAX_ROWS)

        var totalSent = 0
        var outcome: Result? = null
        var guard = 0

        while (outcome == null && guard++ < 1000) {
            val rows = db.peek(BATCH)
            if (rows.isEmpty()) {
                outcome = if (totalSent == 0) Result.Idle else Result.Ok(totalSent, 0)
                break
            }

            val arr = JSONArray()
            rows.forEach { arr.put(JSONObject(it.json)) }

            val req = Request.Builder()
                .url(url)
                .header("Authorization", "Bearer ${s.token}")
                .post(arr.toString().toRequestBody(JSON))
                .build()

            try {
                client.newCall(req).execute().use { resp ->
                    when {
                        resp.isSuccessful -> {
                            db.delete(rows.map { it.id })
                            totalSent += rows.size
                        }

                        resp.code == 401 || resp.code == 403 ->
                            outcome = Result.Failed("afvist (${resp.code}) - tjek token")

                        resp.code in 400..499 -> {
                            // Uafhjælpelig payload-fejl: drop batchen, så køen ikke sætter sig fast.
                            Log.w(TAG, "dropper batch (${resp.code}): ${resp.body?.string()?.take(200)}")
                            db.delete(rows.map { it.id })
                        }

                        else -> outcome = Result.Failed("server ${resp.code}")
                    }
                }
            } catch (e: Exception) {
                Log.w(TAG, "upload fejlede: ${e.message}")
                outcome = if (totalSent > 0) Result.Ok(totalSent, db.count())
                else Result.Failed(e.message ?: "netværksfejl")
            }
        }

        outcome ?: Result.Idle
    }
}
