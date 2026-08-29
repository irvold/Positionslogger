package dk.irvold.positionslogger

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper

/**
 * Lille offline-kø: hver position gemmes som en JSON-linje, indtil den er sendt til serveren.
 * Bevidst uden Room for at holde build'et simpelt.
 */
class QueueDb private constructor(context: Context) :
    SQLiteOpenHelper(context.applicationContext, "queue.db", null, 1) {

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            "CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, " +
                "json TEXT NOT NULL, created_at INTEGER NOT NULL)"
        )
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {}

    fun enqueue(json: String) {
        writableDatabase.insert("outbox", null, ContentValues().apply {
            put("json", json)
            put("created_at", System.currentTimeMillis())
        })
    }

    data class Row(val id: Long, val json: String)

    /** Ældste [limit] rækker (FIFO). */
    fun peek(limit: Int): List<Row> {
        val out = ArrayList<Row>(limit)
        readableDatabase.rawQuery(
            "SELECT id, json FROM outbox ORDER BY id ASC LIMIT ?",
            arrayOf(limit.toString())
        ).use { c ->
            while (c.moveToNext()) out.add(Row(c.getLong(0), c.getString(1)))
        }
        return out
    }

    fun delete(ids: List<Long>) {
        if (ids.isEmpty()) return
        val q = ids.joinToString(",") { "?" }
        writableDatabase.execSQL(
            "DELETE FROM outbox WHERE id IN ($q)",
            ids.map { it.toString() }.toTypedArray()
        )
    }

    fun count(): Int =
        readableDatabase.rawQuery("SELECT COUNT(*) FROM outbox", null).use {
            if (it.moveToFirst()) it.getInt(0) else 0
        }

    /** Undgå ubegrænset vækst hvis serveren er nede i lang tid. */
    fun trimTo(maxRows: Int) {
        writableDatabase.execSQL(
            "DELETE FROM outbox WHERE id NOT IN " +
                "(SELECT id FROM outbox ORDER BY id DESC LIMIT ?)",
            arrayOf(maxRows.toString())
        )
    }

    companion object {
        const val MAX_ROWS = 20_000

        @Volatile private var instance: QueueDb? = null

        fun get(context: Context): QueueDb =
            instance ?: synchronized(this) {
                instance ?: QueueDb(context).also { instance = it }
            }
    }
}
