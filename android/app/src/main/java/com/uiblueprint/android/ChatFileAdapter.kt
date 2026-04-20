package com.uiblueprint.android

import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.CheckBox
import android.widget.ImageButton
import android.widget.PopupMenu
import android.widget.TextView
import androidx.recyclerview.widget.RecyclerView
import java.util.Locale

/**
 * RecyclerView adapter for displaying chat files grouped by category.
 */
class ChatFileAdapter(
    private val listener: FileActionListener,
) : RecyclerView.Adapter<RecyclerView.ViewHolder>() {

    interface FileActionListener {
        fun onToggleIncludeInContext(file: ChatFile, included: Boolean)
        fun onRenameFile(file: ChatFile)
        fun onDeleteFile(file: ChatFile)
        fun onDownloadFile(file: ChatFile)
    }

    private val items = mutableListOf<ListItem>()

    sealed class ListItem {
        data class CategoryHeader(val category: FileCategory) : ListItem()
        data class FileItem(val file: ChatFile) : ListItem()
    }

    class CategoryHeaderViewHolder(view: View) : RecyclerView.ViewHolder(view) {
        val tvCategory: TextView = view.findViewById(R.id.tvCategoryName)
    }

    class FileViewHolder(view: View) : RecyclerView.ViewHolder(view) {
        val cbInclude: CheckBox = view.findViewById(R.id.cbIncludeInContext)
        val tvFileName: TextView = view.findViewById(R.id.tvFileName)
        val tvFileSubtitle: TextView = view.findViewById(R.id.tvFileSubtitle)
        val tvIngestStatus: TextView = view.findViewById(R.id.tvIngestStatus)
        val btnOptions: ImageButton = view.findViewById(R.id.btnFileOptions)
    }

    override fun getItemViewType(position: Int): Int {
        return when (items[position]) {
            is ListItem.CategoryHeader -> VIEW_TYPE_HEADER
            is ListItem.FileItem -> VIEW_TYPE_FILE
        }
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): RecyclerView.ViewHolder {
        return when (viewType) {
            VIEW_TYPE_HEADER -> {
                val view = LayoutInflater.from(parent.context)
                    .inflate(R.layout.item_file_category_header, parent, false)
                CategoryHeaderViewHolder(view)
            }
            else -> {
                val view = LayoutInflater.from(parent.context)
                    .inflate(R.layout.item_chat_file, parent, false)
                FileViewHolder(view)
            }
        }
    }

    override fun onBindViewHolder(holder: RecyclerView.ViewHolder, position: Int) {
        when (val item = items[position]) {
            is ListItem.CategoryHeader -> {
                val vh = holder as CategoryHeaderViewHolder
                vh.tvCategory.text = getCategoryLabel(vh.itemView.context, item.category)
            }
            is ListItem.FileItem -> {
                val vh = holder as FileViewHolder
                val file = item.file

                vh.tvFileName.text = file.filename
                vh.cbInclude.isChecked = file.includedInContext

                // "850 KB - CSV" style subtitle
                val typeLabel = file.category.uppercase(Locale.ROOT)
                vh.tvFileSubtitle.text = "${formatFileSize(file.sizeBytes)} · $typeLabel"

                // Status pill
                vh.tvIngestStatus.bindIngestStatus(file.ingestStatus)

                vh.cbInclude.setOnCheckedChangeListener { _, isChecked ->
                    listener.onToggleIncludeInContext(file, isChecked)
                }

                vh.btnOptions.setOnClickListener { view ->
                    showOptionsMenu(view, file)
                }
            }
        }
    }

    private fun showOptionsMenu(anchor: View, file: ChatFile) {
        val popup = PopupMenu(anchor.context, anchor)
        popup.inflate(R.menu.menu_chat_file_options)
        popup.setOnMenuItemClickListener { menuItem ->
            when (menuItem.itemId) {
                R.id.action_download_file -> {
                    listener.onDownloadFile(file)
                    true
                }
                R.id.action_rename_file -> {
                    listener.onRenameFile(file)
                    true
                }
                R.id.action_delete_file -> {
                    listener.onDeleteFile(file)
                    true
                }
                else -> false
            }
        }
        popup.show()
    }

    private fun getCategoryLabel(context: android.content.Context, category: FileCategory): String {
        return when (category) {
            FileCategory.DOCUMENT -> context.getString(R.string.label_file_category_document)
            FileCategory.CODE -> context.getString(R.string.label_file_category_code)
            FileCategory.IMAGE -> context.getString(R.string.label_file_category_image)
            FileCategory.VIDEO -> context.getString(R.string.label_file_category_video)
            FileCategory.AUDIO -> context.getString(R.string.label_file_category_audio)
            FileCategory.DATA -> context.getString(R.string.label_file_category_data)
            FileCategory.ARCHIVE -> context.getString(R.string.label_file_category_archive)
            FileCategory.OTHER -> context.getString(R.string.label_file_category_other)
        }
    }

    override fun getItemCount(): Int = items.size

    /**
     * Update the file list, grouping by category.
     */
    fun submitList(files: List<ChatFile>) {
        items.clear()

        // Group files by category
        val grouped = files.groupBy { FileCategory.fromString(it.category) }

        // Add category headers and files in order
        val orderedCategories = listOf(
            FileCategory.DOCUMENT,
            FileCategory.CODE,
            FileCategory.IMAGE,
            FileCategory.VIDEO,
            FileCategory.AUDIO,
            FileCategory.DATA,
            FileCategory.ARCHIVE,
            FileCategory.OTHER
        )

        for (category in orderedCategories) {
            val categoryFiles = grouped[category] ?: continue
            if (categoryFiles.isNotEmpty()) {
                items.add(ListItem.CategoryHeader(category))
                categoryFiles.forEach { file ->
                    items.add(ListItem.FileItem(file))
                }
            }
        }

        notifyDataSetChanged()
    }

    companion object {
        private const val VIEW_TYPE_HEADER = 0
        private const val VIEW_TYPE_FILE = 1
    }
}
