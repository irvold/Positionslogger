package dk.irvold.positionslogger

import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Uri
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.TimeUnit

/** Netværks-hjælpere: tjek VPN/forbindelse og åbn Tailscale-appen. */
object Net {

    const val TAILSCALE_PKG = "com.tailscale.ipn"

    private val client = OkHttpClient.Builder()
        .connectTimeout(4, TimeUnit.SECONDS)
        .readTimeout(4, TimeUnit.SECONDS)
        .build()

    /** Er der en aktiv VPN-forbindelse? (Kan ikke se hvilken - men Tailscale er typisk den eneste.) */
    fun isVpnActive(context: Context): Boolean {
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager ?: return false
        val net = cm.activeNetwork ?: return false
        val caps = cm.getNetworkCapabilities(net) ?: return false
        return caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)
    }

    /** Har telefonen overhovedet internet lige nu? */
    fun hasNetwork(context: Context): Boolean {
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager ?: return false
        val caps = cm.getNetworkCapabilities(cm.activeNetwork ?: return false) ?: return false
        return caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
    }

    sealed interface Probe {
        data object Reachable : Probe
        data class HttpError(val code: Int) : Probe
        data class Unreachable(val reason: String) : Probe
        data object NoConfig : Probe
    }

    /** Kalder <server>/api/health for at se om serveren faktisk kan nås. */
    suspend fun probe(context: Context): Probe = withContext(Dispatchers.IO) {
        val url = Prefs.read(context).healthUrl() ?: return@withContext Probe.NoConfig
        try {
            client.newCall(Request.Builder().url(url).get().build()).execute().use { r ->
                if (r.isSuccessful) Probe.Reachable else Probe.HttpError(r.code)
            }
        } catch (e: Exception) {
            Probe.Unreachable(e.message ?: "ingen forbindelse")
        }
    }

    /** Er Tailscale-appen installeret? */
    fun tailscaleInstalled(context: Context): Boolean =
        context.packageManager.getLaunchIntentForPackage(TAILSCALE_PKG) != null

    /**
     * Bedste forsøg på at få Tailscale i gang:
     * 1) bed Tailscale-tjenesten om at forbinde (virker kun hvis VPN allerede er godkendt),
     * 2) ellers åbn Tailscale-appen så brugeren kan trykke "Connect",
     * 3) ellers åbn Google Play.
     */
    fun openTailscale(context: Context) {
        runCatching {
            context.startService(
                Intent().apply {
                    setClassName(TAILSCALE_PKG, "com.tailscale.ipn.IPNService")
                    action = "com.tailscale.ipn.CONNECT_VPN"
                }
            )
        }

        val launch = context.packageManager.getLaunchIntentForPackage(TAILSCALE_PKG)
        if (launch != null) {
            launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            runCatching { context.startActivity(launch) }
            return
        }

        val store = Intent(
            Intent.ACTION_VIEW,
            Uri.parse("https://play.google.com/store/apps/details?id=$TAILSCALE_PKG")
        ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        runCatching { context.startActivity(store) }
    }
}
