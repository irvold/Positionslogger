package dk.irvold.positionslogger

import android.content.Context
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import java.util.concurrent.TimeUnit

/**
 * Sikkerhedsnet: tømmer offline-køen selv når service'en er blevet dræbt,
 * eller når der ikke har været netværk et stykke tid.
 */
class UploadWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        return when (val r = Uploader.flush(applicationContext)) {
            is Uploader.Result.Failed ->
                if (r.reason.contains("token") || r.reason.contains("URL")) Result.success()
                else Result.retry()
            else -> Result.success()
        }
    }

    companion object {
        private const val NAME = "upload-queue"

        fun schedulePeriodic(context: Context) {
            val work = PeriodicWorkRequestBuilder<UploadWorker>(15, TimeUnit.MINUTES)
                .setConstraints(
                    Constraints.Builder()
                        .setRequiredNetworkType(NetworkType.CONNECTED)
                        .build()
                )
                .build()
            WorkManager.getInstance(context)
                .enqueueUniquePeriodicWork(NAME, ExistingPeriodicWorkPolicy.KEEP, work)
        }

        fun cancel(context: Context) {
            WorkManager.getInstance(context).cancelUniqueWork(NAME)
        }
    }
}
