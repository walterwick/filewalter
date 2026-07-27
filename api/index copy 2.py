import os
import boto3
from flask import Flask, render_template_string, request, redirect, url_for, flash, send_file, Response, session
from botocore.exceptions import ClientError
import mimetypes

# --- CONFIGURATION ---
PROFILES = {
    'bucket1': {
        'name': 'Bucket 1',
        'access_key_id': '112b183cdd116461df8ee2d8a647a58c',
        'secret_access_key': 'dd104010783bf0278926182bb9a1d0496c6f62907241d5f196918d7089fe005d',
        'bucket_name': 'walter',
        'endpoint_url': 'https://238a54d3f39bc03c25b5550bbd2683ed.r2.cloudflarestorage.com',
        'region': 'auto',
        'public_url': 'https://s3.walterwick.de/'

    },
    'bucket2': {
        'name': 'Bucket 2',
        'access_key_id': '0a7f0017d897d5b8b982ad26e5711a21',
        'secret_access_key': '9125af83e42fbf5237304c13ea46df52bb1aa5557227fa5f15e8df2fe45f9f31',
        'bucket_name': 'walter',
        'endpoint_url': 'https://a3855f747521f7ef4cea32514f2279e2.r2.cloudflarestorage.com',
        'region': 'auto',
        'public_url': 'https://pub-4dfa55f23513493caa0be91a20704b4c.r2.dev/'
    }
}
DEFAULT_PROFILE = 'bucket1'

app = Flask(__name__)

def get_active_profile_key():
    return session.get('active_profile', DEFAULT_PROFILE)

def get_current_config():
    return PROFILES.get(get_active_profile_key(), PROFILES[DEFAULT_PROFILE])

def get_bucket():
    return get_current_config()['bucket_name']
# Increase max upload size to 16GB (default is usually unlimited but good to be explicit or avoid proxy issues)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 * 1024 
app.secret_key = 'super-secret-key-for-flash-messages'

# --- S3 HELPER ---
def get_s3_client():
    config = get_current_config()
    return boto3.client(
        's3',
        aws_access_key_id=config['access_key_id'],
        aws_secret_access_key=config['secret_access_key'],
        endpoint_url=config['endpoint_url'],
        region_name=config['region']
    )

def list_files(prefix='', continuation_token=''):
    s3 = get_s3_client()
    try:
        kwargs = {'Bucket': get_bucket(), 'Prefix': prefix, 'Delimiter': '/'}
        if continuation_token:
            kwargs['ContinuationToken'] = continuation_token
            
        response = s3.list_objects_v2(**kwargs)
    except ClientError as e:
        return [], [], None, str(e)

    folders = []
    if 'CommonPrefixes' in response:
        for p in response['CommonPrefixes']:
            folder_name = p['Prefix'][len(prefix):]
            folders.append({'name': folder_name, 'path': p['Prefix']})

    files = []
    if 'Contents' in response:
        for obj in response['Contents']:
            key = obj['Key']
            if key == prefix: continue # Skip the folder marker itself
            filename = key[len(prefix):]
            
            # Formate size
            size = obj['Size']
            for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
                if size < 1024:
                    size_str = f"{size:.1f} {unit}"
                    break
                size /= 1024
            
            files.append({
                'name': filename,
                'path': key,
                'size': size_str,
                'last_modified': obj['LastModified']
            })
    
    next_token = response.get('NextContinuationToken')
            
    return folders, files, next_token, None

# --- HTML TEMPLATE ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>S3 Manager Pro</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
      tailwind.config = {
        darkMode: 'media',
        theme: {
          extend: {
            colors: {
              slate: {
                750: '#293548',
                850: '#151e2e',
                950: '#020617',
              }
            }
          }
        }
      }
    </script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
        body { font-family: 'Inter', sans-serif; }
        .drag-area { border: 2px dashed #cbd5e1; transition: all 0.3s ease; }
        .drag-area.active { border-color: #3b82f6; background-color: #eff6ff; }
        @media (prefers-color-scheme: dark) {
            .drag-area { border-color: #475569; }
            .drag-area.active { border-color: #3b82f6; background-color: #1e293b; }
        }
        /* Custom Scrollbar */
        ::-webkit-scrollbar { width: 8px; }
        ::-webkit-scrollbar-track { background: #f1f1f1; }
        ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: #94a3b8; }
    </style>
</head>
<body class="bg-slate-50 text-slate-800 dark:bg-slate-900 dark:text-slate-200 min-h-screen transition-colors duration-200">

    <!-- Top Navigation -->
    <nav class="bg-white dark:bg-slate-800 shadow-sm border-b border-slate-200 dark:border-slate-700 fixed w-full top-0 z-50 transition-colors duration-200">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="flex justify-between h-16">
                <div class="flex">
                    <div class="flex-shrink-0 flex items-center">
                        <a href="/" class="text-2xl font-bold text-blue-600 hover:text-blue-700 transition-colors">
                            <i class="fa-brands fa-aws mr-2"></i>S3 Manager
                        </a>
                    </div>
                </div>
                <div class="flex items-center space-x-4 relative" id="account-dropdown-container">
                    <button onclick="toggleBucketDropdown()" class="text-xs text-slate-500 bg-slate-100 px-3 py-1 rounded-full hover:bg-slate-200 transition-colors flex items-center focus:outline-none dark:bg-slate-700 dark:text-slate-300 dark:hover:bg-slate-600">
                        <i class="fa-solid fa-server mr-1"></i> Bucket: <span class="font-medium text-slate-700 dark:text-slate-200 ml-1">{{ current_profile_name }}</span> 
                        <i class="fa-solid fa-chevron-down ml-2 text-[10px]"></i>
                    </button>
                    <div id="bucketMenu" class="hidden absolute right-0 top-full mt-2 w-48 bg-white dark:bg-slate-800 rounded-lg shadow-lg py-2 z-50 border border-slate-100 dark:border-slate-700">
                        <a href="{{ url_for('set_bucket', name='bucket1') }}" class="block px-4 py-2 text-sm text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-700 hover:text-blue-600 dark:hover:text-blue-400">
                            <i class="fa-solid fa-database mr-2 text-slate-400"></i>Bucket 1
                        </a>
                        <a href="{{ url_for('set_bucket', name='bucket2') }}" class="block px-4 py-2 text-sm text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-700 hover:text-blue-600 dark:hover:text-blue-400">
                            <i class="fa-solid fa-database mr-2 text-slate-400"></i>Bucket 2
                        </a>
                    </div>
                </div>
            </div>
        </div>
    </nav>

    <!-- Main Content -->
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 pt-24 pb-12">
        
        <!-- Flash Messages -->
        {% with messages = get_flashed_messages(with_categories=true) %}
          {% if messages %}
            <div class="mb-6">
              {% for category, message in messages %}
                <div class="p-4 mb-2 rounded-lg shadow-sm border
                    {% if category == 'error' %} bg-red-50 dark:bg-red-900/20 border-red-200 dark:border-red-900/50 text-red-700 dark:text-red-400
                    {% else %} bg-green-50 dark:bg-green-900/20 border-green-200 dark:border-green-900/50 text-green-700 dark:text-green-400 {% endif %} flex items-center">
                    <i class="fa-solid {% if category == 'error' %}fa-circle-exclamation{% else %}fa-circle-check{% endif %} mr-2"></i>
                    {{ message }}
                </div>
              {% endfor %}
            </div>
          {% endif %}
        {% endwith %}

        <!-- Actions Bar -->
        <div class="flex flex-col md:flex-row md:items-center justify-between gap-4 mb-6">
            <!-- Breadcrumbs -->
            <nav class="flex text-slate-500 dark:text-slate-400 overflow-x-auto whitespace-nowrap pb-1 md:pb-0">
                <a href="{{ url_for('index') }}" class="hover:text-blue-600 dark:hover:text-blue-400 transition-colors flex items-center">
                    <i class="fa-solid fa-house mr-1"></i> Home
                </a>
                {% set parts = prefix.strip('/').split('/') %}
                {% set current_path = namespace(value='') %}
                {% if prefix %}
                    {% for part in parts %}
                        {% if part %}
                            {% set current_path.value = current_path.value + part + '/' %}
                            <span class="mx-2 text-slate-300 dark:text-slate-600">/</span>
                            <a href="{{ url_for('index', prefix=current_path.value) }}" class="font-medium hover:text-blue-600 dark:hover:text-blue-400 transition-colors text-slate-700 dark:text-slate-300">
                                {{ part }}
                            </a>
                        {% endif %}
                    {% endfor %}
                {% endif %}
            </nav>

            <div class="flex items-center space-x-2">
                <button onclick="document.getElementById('newFolderModal').classList.remove('hidden')" class="bg-white dark:bg-slate-700 border border-slate-300 dark:border-slate-600 text-slate-700 dark:text-slate-200 px-4 py-2 rounded-lg hover:bg-slate-50 dark:hover:bg-slate-600 hover:text-blue-600 dark:hover:text-blue-400 transition-all text-sm font-medium shadow-sm">
                    <i class="fa-solid fa-folder-plus mr-2"></i>New Folder
                </button>
                
                <!-- Upload Buttons Group -->
                <div class="relative inline-block text-left group">
                    <button class="bg-blue-600 text-white px-4 py-2 rounded-lg hover:bg-blue-700 transition-all text-sm font-medium shadow-md hover:shadow-lg flex items-center">
                        <i class="fa-solid fa-cloud-arrow-up mr-2"></i>Upload
                        <i class="fa-solid fa-chevron-down ml-2 text-xs"></i>
                    </button>
                    <!-- Dropdown -->
                    <div class="hidden group-hover:block absolute right-0 mt-0 w-48 bg-white dark:bg-slate-800 rounded-md shadow-lg py-1 z-20 border border-slate-100 dark:border-slate-700">
                        <a href="#" onclick="document.getElementById('uploadModal').classList.remove('hidden'); setupUpload('file')" class="block px-4 py-2 text-sm text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-700">
                            <i class="fa-solid fa-file mr-2 text-slate-400"></i> Upload Files
                        </a>
                        <a href="#" onclick="document.getElementById('uploadModal').classList.remove('hidden'); setupUpload('folder')" class="block px-4 py-2 text-sm text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-700">
                            <i class="fa-solid fa-folder mr-2 text-slate-400"></i> Upload Folder
                        </a>
                    </div>
                </div>
            </div>
        </div>

        <!-- File Browser -->
        <div class="bg-white dark:bg-slate-800 rounded-xl shadow-sm border border-slate-200 dark:border-slate-700 overflow-hidden mb-6 transition-colors duration-200">
            {% if not folders and not files %}
                <div class="p-12 text-center text-slate-400 dark:text-slate-500">
                    <i class="fa-regular fa-folder-open text-6xl mb-4 text-slate-200 dark:text-slate-700"></i>
                    <p class="text-lg">This folder is empty</p>
                    <p class="text-sm mt-2">Upload files or create a new folder to get started.</p>
                </div>
            {% else %}
                <div class="overflow-x-auto">
                    <table class="w-full text-left text-sm text-slate-600 dark:text-slate-300">
                        <thead class="bg-slate-50 dark:bg-slate-750 text-xs uppercase font-semibold text-slate-500 dark:text-slate-400 border-b border-slate-200 dark:border-slate-700">
                            <tr>
                                <th class="px-6 py-4 rounded-tl-lg">Name</th>
                                <th class="px-6 py-4">Size</th>
                                <th class="px-6 py-4">Last Modified</th>
                                <th class="px-6 py-4 text-right rounded-tr-lg">Actions</th>
                            </tr>
                        </thead>
                        <tbody class="divide-y divide-slate-100 dark:divide-slate-700">
                            <!-- Folders -->
                            {% for folder in folders %}
                            <tr class="hover:bg-slate-50 dark:hover:bg-slate-750 transition-colors group border-b border-slate-100 dark:border-slate-700 last:border-0">
                                <td class="px-6 py-3 whitespace-nowrap cursor-pointer" onclick="window.location.href='{{ url_for('index', prefix=folder.path) }}'">
                                    <div class="flex items-center text-slate-700 dark:text-slate-200 font-medium">
                                        <i class="fa-solid fa-folder text-yellow-400 text-2xl mr-3"></i>
                                        {{ folder.name }}
                                    </div>
                                </td>
                                <td class="px-6 py-3 text-slate-400">-</td>
                                <td class="px-6 py-3 text-slate-400">-</td>
                                <td class="px-6 py-3 text-right space-x-1">
                                    <button onclick="event.stopPropagation(); openRenameModal('{{ folder.path }}')" class="text-slate-400 hover:text-orange-500 px-2 py-1 rounded transition-colors" title="Rename Folder">
                                        <i class="fa-solid fa-pen-to-square"></i>
                                    </button>
                                     <form action="{{ url_for('delete') }}" method="POST" class="inline" onsubmit="return confirm('Delete folder {{ folder.name }} and all its contents?');">
                                        <input type="hidden" name="key" value="{{ folder.path }}">
                                        <input type="hidden" name="prefix" value="{{ prefix }}">
                                        <button type="submit" class="text-slate-400 hover:text-red-500 px-2 py-1 rounded transition-colors" title="Delete Folder">
                                            <i class="fa-regular fa-trash-can"></i>
                                        </button>
                                    </form>
                                </td>
                            </tr>
                            {% endfor %}

                            <!-- Files -->
                            {% for file in files %}
                            <tr class="hover:bg-slate-50 dark:hover:bg-slate-750 transition-colors group border-b border-slate-100 dark:border-slate-700 last:border-0">
                                <td class="px-6 py-3 font-medium text-slate-700 dark:text-slate-200">
                                    <div class="flex items-center space-x-3">
                                        <!-- Preview Click -->
                                        <!-- Preview Click -->
                                        <button onclick="openPreview('{{ file.path }}', '{{ file.name }}')" class="w-8 h-8 rounded-lg bg-slate-100 dark:bg-slate-700 text-slate-500 dark:text-slate-400 hover:text-blue-600 dark:hover:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-900/30 flex items-center justify-center text-lg transition-colors" title="Preview">
                                           {% if file.name.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')) %} 
                                                <i class="fa-regular fa-image"></i>
                                           {% elif file.name.lower().endswith(('.pdf')) %}
                                                <i class="fa-regular fa-file-pdf text-red-500"></i>
                                           {% elif file.name.lower().endswith(('.mp4', '.webm', '.mov')) %}
                                                <i class="fa-regular fa-file-video text-blue-500"></i>
                                           {% else %}
                                                <i class="fa-regular fa-file"></i>
                                           {% endif %}
                                        </button>
                                        
                                        <!-- New Tab Click -->
                                        <button onclick="openInNewTab('{{ file.path }}')" class="hover:text-blue-600 dark:hover:text-blue-400 hover:underline text-left">
                                            {{ file.name }}
                                        </button>
                                    </div>
                                </td>
                                <td class="px-6 py-3">{{ file.size }}</td>
                                <td class="px-6 py-3">{{ file.last_modified.strftime('%Y-%m-%d %H:%M') }}</td>
                                <td class="px-6 py-3 text-right space-x-1">
                                    <button onclick="openRenameModal('{{ file.path }}')" class="text-slate-400 hover:text-orange-500 px-2 py-1 rounded transition-colors" title="Rename/Move">
                                        <i class="fa-solid fa-pen-to-square"></i>
                                    </button>
                                    <button onclick="copyLink('{{ file.path }}')" class="text-slate-400 hover:text-green-600 px-2 py-1 rounded transition-colors" title="Copy Link">
                                        <i class="fa-solid fa-link"></i>
                                    </button>
                                    <a href="{{ url_for('download', key=file.path) }}" class="text-slate-400 hover:text-blue-600 px-2 py-1 rounded transition-colors" title="Download">
                                        <i class="fa-solid fa-download"></i>
                                    </a>
                                    <form action="{{ url_for('delete') }}" method="POST" class="inline" onsubmit="return confirm('Delete file {{ file.name }}?');">
                                        <input type="hidden" name="key" value="{{ file.path }}">
                                        <input type="hidden" name="prefix" value="{{ prefix }}">
                                        <button type="submit" class="text-slate-400 hover:text-red-500 px-2 py-1 rounded transition-colors" title="Delete">
                                            <i class="fa-regular fa-trash-can"></i>
                                        </button>
                                    </form>
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            {% endif %}
        </div>
        
        <!-- Infinite Scroll Sentinel -->
        <div id="sentinel" data-next-token="{{ next_token if next_token else '' }}" class="h-20 text-center py-8">
            {% if next_token %}
             <div id="loadingSpinner" class="text-slate-400 text-sm">
                <i class="fa-solid fa-circle-notch fa-spin mr-2"></i> Loading more files...
             </div>
            {% endif %}
        </div>
    </div>

    <!-- Upload Modal -->
    <div id="uploadModal" class="hidden fixed inset-0 z-50 overflow-y-auto" aria-labelledby="modal-title" role="dialog" aria-modal="true">
        <div class="flex items-end justify-center min-h-screen pt-4 px-4 pb-20 text-center sm:block sm:p-0">
            <div class="fixed inset-0 bg-slate-900 bg-opacity-75 transition-opacity" aria-hidden="true" onclick="handleModalClose()"></div>
            <span class="hidden sm:inline-block sm:align-middle sm:h-screen" aria-hidden="true">&#8203;</span>
            <div class="inline-block align-bottom bg-white dark:bg-slate-800 rounded-xl text-left overflow-hidden shadow-xl transform transition-all sm:my-8 sm:align-middle sm:max-w-lg sm:w-full">
                <div class="bg-white dark:bg-slate-800 px-4 pt-5 pb-4 sm:p-6 sm:pb-4">
                    <h3 class="text-lg leading-6 font-medium text-slate-900 dark:text-white mb-4" id="uploadModalTitle">Upload File</h3>
                    
                    <!-- Custom Path Input -->
                    <div class="mb-4">
                        <label class="block text-xs font-medium text-slate-500 dark:text-slate-400 uppercase mb-1">Upload To (Folder Path)</label>
                        <div class="relative rounded-md shadow-sm">
                            <div class="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
                                <span class="text-slate-400 sm:text-sm">/</span>
                            </div>
                            <input type="text" id="targetPathInput" class="focus:ring-blue-500 focus:border-blue-500 block w-full pl-7 sm:text-sm border-slate-300 dark:border-slate-600 rounded-md border p-2 bg-white dark:bg-slate-700 text-slate-900 dark:text-white placeholder-slate-400" placeholder="folder/subfolder">
                        </div>
                        <p class="text-xs text-slate-400 mt-1">Leave empty for root, or type a folder path (e.g. 'images/2024'). Will be created if it doesn't exist.</p>
                    </div>

                    <div id="uploadUi">
                        <div class="drag-area w-full h-40 rounded-xl bg-slate-50 dark:bg-slate-700/50 flex flex-col items-center justify-center text-slate-400 dark:text-slate-500 mb-4 cursor-pointer relative" id="dragArea">
                            <input type="file" class="absolute inset-0 w-full h-full opacity-0 cursor-pointer z-10" id="fileInput" onchange="filesSelected(this)" multiple>
                            <i class="fa-solid fa-cloud-arrow-up text-4xl mb-2"></i>
                            <p class="text-sm font-medium" id="dropText">Drag & Drop or Click to Browse</p>
                            <p class="text-xs text-slate-400 mt-1" id="fileStats"></p>
                        </div>
                    </div>
                    
                    <!-- Total Progress -->
                    <div id="totalProgress" class="hidden mb-6 bg-blue-50 dark:bg-blue-900/20 p-4 rounded-xl border border-blue-100 dark:border-blue-900/30">
                        <div class="flex justify-between text-sm font-bold text-blue-900 dark:text-blue-300 mb-2">
                            <span id="totalText">Preparing...</span>
                            <span id="totalPercent">0%</span>
                        </div>
                        <div class="w-full bg-blue-200 dark:bg-blue-900/40 rounded-full h-3">
                            <div id="totalBar" class="bg-blue-600 dark:bg-blue-500 h-3 rounded-full transition-all duration-300 shadow-sm" style="width: 0%"></div>
                        </div>
                    </div>

                    <!-- Individual Progress Container -->
                    <div id="progressContainer" class="max-h-60 overflow-y-auto hidden space-y-3 mb-4 pr-2">
                        <!-- Progress items will be injected here -->
                    </div>

                    <div class="mt-5 sm:mt-6 sm:grid sm:grid-cols-2 sm:gap-3 sm:grid-flow-row-dense">
                        <button type="button" id="startUploadBtn" class="hidden w-full inline-flex justify-center rounded-lg border border-transparent shadow-sm px-4 py-2 bg-green-600 text-base font-medium text-white hover:bg-green-700 focus:outline-none sm:col-start-2 sm:text-sm" onclick="startUpload()">
                            Start Upload
                        </button>
                        <button type="button" id="closeBtn" class="w-full inline-flex justify-center rounded-lg border border-slate-300 dark:border-slate-600 shadow-sm px-4 py-2 bg-white dark:bg-slate-700 text-base font-medium text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-600 focus:outline-none sm:col-start-1 sm:text-sm" onclick="handleModalClose()">
                            Close
                        </button>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Rename Modal -->
    <div id="renameModal" class="hidden fixed inset-0 z-50 overflow-y-auto">
        <div class="flex items-end justify-center min-h-screen pt-4 px-4 pb-20 text-center sm:block sm:p-0">
            <div class="fixed inset-0 bg-slate-900 bg-opacity-75" onclick="document.getElementById('renameModal').classList.add('hidden')"></div>
            <span class="hidden sm:inline-block sm:align-middle sm:h-screen">&#8203;</span>
            <div class="inline-block align-bottom bg-white dark:bg-slate-800 rounded-xl text-left overflow-hidden shadow-xl transform transition-all sm:my-8 sm:align-middle sm:max-w-lg sm:w-full">
                <div class="bg-white dark:bg-slate-800 px-4 pt-5 pb-4 sm:p-6 sm:pb-4">
                    <h3 class="text-lg leading-6 font-medium text-slate-900 dark:text-white mb-4">Rename / Move File</h3>
                    <form action="{{ url_for('rename') }}" method="POST">
                        <input type="hidden" name="old_key" id="renameOldKey">
                        <div class="mb-4">
                            <label for="renameNewKey" class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">New Path (Folder + Filename)</label>
                            <input type="text" name="new_key" id="renameNewKey" class="shadow-sm focus:ring-blue-500 focus:border-blue-500 block w-full sm:text-sm border-slate-300 dark:border-slate-600 rounded-md p-2 border font-mono text-sm bg-white dark:bg-slate-700 text-slate-900 dark:text-white" required>
                            <p class="text-xs text-slate-500 dark:text-slate-400 mt-2">Example: <code>folder/newname.jpg</code> or just <code>newname.jpg</code> to move to root.</p>
                        </div>
                        <div class="mt-5 sm:mt-6 sm:grid sm:grid-cols-2 sm:gap-3 sm:grid-flow-row-dense">
                            <button type="submit" class="w-full inline-flex justify-center rounded-lg border border-transparent shadow-sm px-4 py-2 bg-orange-600 text-base font-medium text-white hover:bg-orange-700 focus:outline-none sm:col-start-2 sm:text-sm">
                                Rename / Move
                            </button>
                            <button type="button" class="mt-3 w-full inline-flex justify-center rounded-lg border border-slate-300 dark:border-slate-600 shadow-sm px-4 py-2 bg-white dark:bg-slate-700 text-base font-medium text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-600 focus:outline-none sm:mt-0 sm:col-start-1 sm:text-sm" onclick="document.getElementById('renameModal').classList.add('hidden')">
                                Cancel
                            </button>
                        </div>
                    </form>
                </div>
            </div>
        </div>
    </div>

    <!-- Preview Modal -->
    <div id="previewModal" class="hidden fixed inset-0 z-50 overflow-y-auto">
        <div class="flex items-center justify-center min-h-screen px-4 pt-4 pb-20 text-center sm:block sm:p-0">
            <div class="fixed inset-0 bg-slate-900 bg-opacity-90 transition-opacity" onclick="closePreview()"></div>
            <span class="hidden sm:inline-block sm:align-middle sm:h-screen">&#8203;</span>
            <div class="inline-block align-middle bg-transparent rounded-lg text-left overflow-hidden shadow-xl transform transition-all sm:max-w-4xl sm:w-full relative group">
                
                 <!-- Nav Buttons (Desktop) -->
                 <button onclick="showPrevPreview()" class="absolute left-0 top-1/2 -translate-y-1/2 text-white/50 hover:text-white text-4xl p-4 hidden md:block z-50 focus:outline-none">
                    <i class="fa-solid fa-chevron-left"></i>
                 </button>
                 <button onclick="showNextPreview()" class="absolute right-0 top-1/2 -translate-y-1/2 text-white/50 hover:text-white text-4xl p-4 hidden md:block z-50 focus:outline-none">
                    <i class="fa-solid fa-chevron-right"></i>
                 </button>

                <div class="relative bg-white dark:bg-slate-800 rounded-lg overflow-hidden">
                    <div class="flex justify-between items-center p-4 border-b border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-750">
                        <h3 class="text-lg font-medium text-slate-900 dark:text-white truncate pr-4 flex-1" id="previewTitle">File Preview</h3>
                        <div class="flex items-center space-x-2">
                             <a href="#" id="previewOpenBtn" target="_blank" class="text-slate-400 hover:text-blue-600 px-3 py-1 rounded hover:bg-slate-100 dark:hover:bg-slate-700 transition-colors" title="Open in New Tab">
                                <i class="fa-solid fa-arrow-up-right-from-square"></i>
                            </a>
                            <button type="button" onclick="deleteCurrentPreview()" class="text-slate-400 hover:text-red-500 px-3 py-1 rounded hover:bg-slate-100 dark:hover:bg-slate-700 transition-colors" title="Delete File">
                                <i class="fa-regular fa-trash-can"></i>
                            </button>
                            <div class="w-px h-6 bg-slate-200 dark:bg-slate-600 mx-1"></div>
                            <button type="button" class="text-slate-400 hover:text-slate-500 dark:hover:text-slate-300 focus:outline-none" onclick="closePreview()">
                                <span class="sr-only">Close</span>
                               <i class="fa-solid fa-xmark text-xl"></i>
                            </button>
                        </div>
                    </div>
                    <div class="p-4 bg-slate-100 dark:bg-slate-900 flex justify-center items-center min-h-[300px] max-h-[80vh] overflow-auto" id="previewContent">
                        <!-- Content injected by JS -->
                        <i class="fa-solid fa-circle-notch fa-spin text-4xl text-blue-500"></i>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- New Folder Modal -->
    <div id="newFolderModal" class="hidden fixed inset-0 z-50 overflow-y-auto">
        <div class="flex items-end justify-center min-h-screen pt-4 px-4 pb-20 text-center sm:block sm:p-0">
            <div class="fixed inset-0 bg-slate-900 bg-opacity-75" onclick="document.getElementById('newFolderModal').classList.add('hidden')"></div>
            <span class="hidden sm:inline-block sm:align-middle sm:h-screen">&#8203;</span>
            <div class="inline-block align-bottom bg-white dark:bg-slate-800 rounded-xl text-left overflow-hidden shadow-xl transform transition-all sm:my-8 sm:align-middle sm:max-w-lg sm:w-full">
                <div class="bg-white dark:bg-slate-800 px-4 pt-5 pb-4 sm:p-6 sm:pb-4">
                    <h3 class="text-lg leading-6 font-medium text-slate-900 dark:text-white mb-4">Create New Folder</h3>
                    <form action="{{ url_for('mkdir') }}" method="POST">
                        <input type="hidden" name="prefix" value="{{ prefix }}">
                        <div class="mb-4">
                            <label for="folderName" class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">Folder Name</label>
                            <input type="text" name="folder_name" id="folderName" class="shadow-sm focus:ring-blue-500 focus:border-blue-500 block w-full sm:text-sm border-slate-300 dark:border-slate-600 rounded-md p-2 border bg-white dark:bg-slate-700 text-slate-900 dark:text-white placeholder-slate-400" placeholder="e.g., images" required>
                        </div>
                        <div class="mt-5 sm:mt-6 sm:grid sm:grid-cols-2 sm:gap-3 sm:grid-flow-row-dense">
                            <button type="submit" class="w-full inline-flex justify-center rounded-lg border border-transparent shadow-sm px-4 py-2 bg-blue-600 text-base font-medium text-white hover:bg-blue-700 focus:outline-none sm:col-start-2 sm:text-sm">
                                Create
                            </button>
                            <button type="button" class="mt-3 w-full inline-flex justify-center rounded-lg border border-slate-300 dark:border-slate-600 shadow-sm px-4 py-2 bg-white dark:bg-slate-700 text-base font-medium text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-600 focus:outline-none sm:mt-0 sm:col-start-1 sm:text-sm" onclick="document.getElementById('newFolderModal').classList.add('hidden')">
                                Cancel
                            </button>
                        </div>
                    </form>
                </div>
            </div>
        </div>
    </div>

    <!-- Toast Notification -->
    <div id="toast" class="fixed bottom-4 right-4 bg-slate-800 text-white px-6 py-3 rounded-lg shadow-lg transform translate-y-20 opacity-0 transition-all duration-300 z-50">
        <i class="fa-solid fa-circle-check text-green-400 mr-2"></i>
        <span id="toastMessage">Notification</span>
    </div>

    <script>
        const PREFIX = new URLSearchParams(window.location.search).get('prefix') || '';
        let totalFiles = 0;
        let uploadedFiles = 0;
        let successfulUploadsCount = 0; // New tracker for reload decision
        
        // Concurrency Control
        const MAX_CONCURRENT_UPLOADS = 3;
        let activeUploads = 0;
        let uploadQueue = [];
        let isUploading = false; // Flag to check if start button pressed

        // --- INIT ---
        function toggleBucketDropdown() {
             const menu = document.getElementById('bucketMenu');
             menu.classList.toggle('hidden');
        }
        
        window.addEventListener('click', function(e) {
            const container = document.getElementById('account-dropdown-container');
            const menu = document.getElementById('bucketMenu');
            if (container && !container.contains(e.target)) {
                 if (menu && !menu.classList.contains('hidden')) {
                    menu.classList.add('hidden');
                 }
            }
        });

        document.addEventListener('DOMContentLoaded', () => {
             setupInfiniteScroll();
             setupPaste();
        });

        // --- INFINITE SCROLL ---
        function setupInfiniteScroll() {
            const sentinel = document.getElementById('sentinel');
            if (!sentinel) return;

            const observer = new IntersectionObserver((entries) => {
                if (entries[0].isIntersecting) {
                    const nextToken = sentinel.dataset.nextToken;
                    if (nextToken) {
                        loadMoreFiles(nextToken);
                    }
                }
            });
            observer.observe(sentinel);
        }

        async function loadMoreFiles(token) {
            const sentinel = document.getElementById('sentinel');
            const spinner = document.getElementById('loadingSpinner');
            
            // Prevent multiple triggers
            if (sentinel.dataset.loading === 'true') return;
            sentinel.dataset.loading = 'true';

            try {
                // Construct URL for next page
                const url = new URL(window.location.href);
                url.searchParams.set('continuation_token', token);
                
                const res = await fetch(url.toString());
                const text = await res.text();
                const parser = new DOMParser();
                const doc = parser.parseFromString(text, 'text/html');
                
                // Extract new rows
                const newRows = doc.querySelectorAll('tbody tr');
                const tbody = document.querySelector('tbody');
                newRows.forEach(row => tbody.appendChild(row));
                
                // Update Token
                const newSentinel = doc.getElementById('sentinel');
                const newNextToken = newSentinel ? newSentinel.dataset.nextToken : null;
                
                if (newNextToken) {
                    sentinel.dataset.nextToken = newNextToken;
                    sentinel.dataset.loading = 'false';
                } else {
                    sentinel.removeAttribute('data-next-token');
                    sentinel.innerHTML = '<span class="text-xs text-slate-300">End of list</span>';
                    // Stop observing
                }

            } catch (err) {
                console.error("Scroll failed", err);
                sentinel.dataset.loading = 'false';
                 // Retry logic could go here
            }
        }

        // --- PASTE SUPPORT ---
        function setupPaste() {
            document.addEventListener('paste', (e) => {
                const items = e.clipboardData.items;
                const fileList = [];
                
                for (let i = 0; i < items.length; i++) {
                   if (items[i].kind === 'file') {
                       const file = items[i].getAsFile();
                       if (file) fileList.push(file);
                   }
                }
                
                if (fileList.length > 0) {
                    // Open modal if not open? Or just queue if open?
                    // Let's open modal and start
                     document.getElementById('uploadModal').classList.remove('hidden');
                     
                     // Mock input-like object
                     const mockInput = { files: fileList };
                     filesSelected(mockInput);
                }
            });
        }

        function setupUpload(type) {
            const title = document.getElementById('uploadModalTitle');
            const input = document.getElementById('fileInput');
            const dropText = document.getElementById('dropText');
            
            // Reset state
            activeUploads = 0;
            uploadQueue = [];
            isUploading = false;
            totalFiles = 0;
            uploadedFiles = 0;
            successfulUploadsCount = 0;
            
            document.getElementById('uploadUi').classList.remove('hidden');
            document.getElementById('progressContainer').classList.add('hidden');
            document.getElementById('progressContainer').innerHTML = ''; // Clear items
            document.getElementById('totalProgress').classList.add('hidden');
            document.getElementById('startUploadBtn').classList.add('hidden');
            document.getElementById('closeBtn').classList.remove('bg-green-600', 'text-white', 'hover:bg-green-700');
            document.getElementById('closeBtn').classList.add('bg-white', 'text-slate-700', 'border-slate-300');
            document.getElementById('closeBtn').textContent = "Close";
            
            document.getElementById('targetPathInput').value = PREFIX;
            
            const newInput = input.cloneNode(true);
            input.parentNode.replaceChild(newInput, input);
            newInput.addEventListener('change', function() { filesSelected(this); });
            
            if (type === 'folder') {
                title.textContent = 'Upload Folder';
                dropText.textContent = 'Drag & Drop a Folder here';
                newInput.setAttribute('webkitdirectory', '');
                newInput.setAttribute('directory', '');
            } else {
                title.textContent = 'Upload Files';
                dropText.textContent = 'Drag & Drop Files here or Paste (Ctrl+V)';
                newInput.removeAttribute('webkitdirectory');
                newInput.removeAttribute('directory');
            }
            
            document.getElementById('uploadModal').classList.remove('hidden');
        }

        async function filesSelected(input) {
            if (!input.files || input.files.length === 0) return;
            
            const files = Array.from(input.files);
            
            document.getElementById('uploadUi').classList.add('hidden');
            document.getElementById('totalProgress').classList.remove('hidden'); 
            document.getElementById('progressContainer').classList.remove('hidden');
            document.getElementById('startUploadBtn').classList.remove('hidden'); // Show START
            
            // Add to queue
            const container = document.getElementById('progressContainer');
            
            for (let i = 0; i < files.length; i++) {
                const file = files[i];
                const id = 'prog-' + Date.now() + '-' + i;
                
                // Track total
                totalFiles++;
                
                const el = document.createElement('div');
                el.id = 'el-' + id;
                el.innerHTML = `
                    <div class="text-xs text-slate-500 flex justify-between mb-1 items-center">
                        <div class="truncate w-3/4 font-mono flex items-center">
                            <span class="mr-2 cursor-pointer text-red-400 hover:text-red-600 remove-btn" onclick="removeFromQueue('${id}')"><i class="fa-solid fa-xmark"></i></span>
                            ${file.name}
                        </div>
                        <div class="text-right">
                             <span id="${id}-speed" class="text-slate-300 mr-2 text-[10px]"></span>
                             <span id="${id}-pct">Pending</span>
                        </div>
                    </div>
                    <div class="w-full bg-slate-200 rounded-full h-2">
                        <div id="${id}-bar" class="bg-slate-300 h-2 rounded-full transition-all duration-300" style="width: 0%"></div>
                    </div>
                `;
                container.appendChild(el);
                
                uploadQueue.push({ 
                    file, 
                    id, 
                    retries: 0,
                    lastLoaded: 0,
                    lastTime: Date.now()
                });
            }
            
            updateTotalProgress();
        }
        
        function removeFromQueue(id) {
            if (isUploading) return; 
            
            const idx = uploadQueue.findIndex(item => item.id === id);
            if (idx > -1) {
                uploadQueue.splice(idx, 1);
                document.getElementById('el-' + id).remove();
                totalFiles--;
                updateTotalProgress();
                
                if (totalFiles === 0) {
                     // Reset UI if empty
                     document.getElementById('uploadUi').classList.remove('hidden');
                     document.getElementById('totalProgress').classList.add('hidden');
                     document.getElementById('startUploadBtn').classList.add('hidden');
                }
            }
        }
        
        function startUpload() {
            if (uploadQueue.length === 0) return;
            
            isUploading = true;
            document.getElementById('startUploadBtn').classList.add('hidden');
            
            // Hide remove buttons
            const removeBtns = document.querySelectorAll('.remove-btn');
            removeBtns.forEach(el => el.style.display = 'none');
            
            processQueue();
        }
        
        function updateTotalProgress() {
            // Prevent NaN
            const pct = totalFiles > 0 ? Math.round((uploadedFiles / totalFiles) * 100) : 0;
            const bar = document.getElementById('totalBar');
            const text = document.getElementById('totalText');
            const pctText = document.getElementById('totalPercent');
            
            // Cap at 100% just in case
            const displayPct = Math.min(pct, 100);
            
            bar.style.width = displayPct + '%';
            text.textContent = `Uploaded ${uploadedFiles} of ${totalFiles} files`;
            pctText.textContent = displayPct + '%';
            
            if (uploadedFiles >= totalFiles && totalFiles > 0 && isUploading) {
                bar.classList.add('bg-green-500');
            } else {
                 bar.classList.remove('bg-green-500');
            }
        }

        function processQueue() {
            // Keep going as long as we have concurrency slots and items
            while (activeUploads < MAX_CONCURRENT_UPLOADS && uploadQueue.length > 0) {
                const item = uploadQueue.shift();
                activeUploads++;
                uploadFile(item);
            }
            
            if (uploadQueue.length === 0 && activeUploads === 0 && isUploading) {
                checkAllDone();
            }
        }

        async function uploadFile(item) {
            const { file, id } = item;
            const bar = document.getElementById(id + '-bar');
            const pct = document.getElementById(id + '-pct');
            const speedEl = document.getElementById(id + '-speed');
            
            // Read TARGET PATH from input
            let targetPrefix = document.getElementById('targetPathInput').value.trim();
            if (targetPrefix && !targetPrefix.endsWith('/')) targetPrefix += '/';
            if (targetPrefix.startsWith('/')) targetPrefix = targetPrefix.substring(1);
            
            bar.classList.remove('bg-slate-300');
            bar.classList.add('bg-blue-600');
            pct.textContent = 'Starting...';

            let filename = file.name;
            if (file.webkitRelativePath) {
                filename = file.webkitRelativePath;
            }
            
            try {
                // 1. Get Presigned URL
                const res = await fetch('/get_upload_link', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ 
                        filename: filename,
                        prefix: targetPrefix 
                    })
                });
                
                if (res.status === 409) {
                     activeUploads--;
                     uploadedFiles++; 
                     
                     bar.classList.remove('bg-blue-600');
                     bar.classList.add('bg-yellow-400');
                     bar.style.width = '100%';
                     pct.innerHTML = '<span class="text-yellow-600 font-bold">Skipped</span>';
                     speedEl.textContent = '';
                     
                     updateTotalProgress();
                     processQueue();
                     return;
                }
                
                if (!res.ok) throw new Error('Sign fail');
                const data = await res.json();
                
                // 2. Direct PUT to S3
                const xhr = new XMLHttpRequest();
                xhr.open('PUT', data.url, true);
                xhr.setRequestHeader('Content-Type', data.content_type);
                
                xhr.upload.onprogress = (e) => {
                    if (e.lengthComputable) {
                        const now = Date.now();
                        const timeDiff = (now - item.lastTime) / 1000; // seconds
                        
                        if (timeDiff >= 0.5) { // Update speed every 0.5s
                            const loadedDiff = e.loaded - item.lastLoaded;
                            const speedBytes = loadedDiff / timeDiff;
                            const speedMB = (speedBytes / (1024 * 1024)).toFixed(1);
                            
                            speedEl.textContent = `${speedMB} MB/s`;
                            
                            item.lastLoaded = e.loaded;
                            item.lastTime = now;
                        }

                        const percent = Math.round((e.loaded / e.total) * 100);
                        bar.style.width = percent + '%';
                        pct.textContent = percent + '%';
                    }
                };
                
                xhr.onload = () => {
                    activeUploads--;
                    if (xhr.status === 200) {
                        bar.classList.add('bg-green-500');
                        bar.classList.remove('bg-blue-600');
                        pct.innerHTML = '<i class="fa-solid fa-check text-green-500"></i>';
                        speedEl.textContent = '';
                        
                        // Increment counts ONLY ON SUCCESS
                        uploadedFiles++;
                        successfulUploadsCount++;
                        updateTotalProgress();
                        processQueue(); 
                    } else {
                        handleError(item, 'HTTP ' + xhr.status);
                    }
                };
                
                xhr.onerror = () => {
                     activeUploads--;
                     handleError(item, 'Net Err');
                };
                
                xhr.send(file);
                
            } catch (err) {
                activeUploads--;
                handleError(item, 'Fail');
            }
        }

        function handleError(item, msg) {
            const { id, retries } = item;
            const bar = document.getElementById(id + '-bar');
            const pct = document.getElementById(id + '-pct');
             const speedEl = document.getElementById(id + '-speed');
             speedEl.textContent = '';

            if (retries < 3) {
                pct.textContent = `Retry ${retries+1}...`;
                // Reset stats for retry
                item.retries++;
                item.lastLoaded = 0;
                item.lastTime = Date.now();
                
                uploadQueue.unshift(item); 
                setTimeout(() => {
                    processQueue(); 
                }, 1000);
            } else {
                bar.classList.remove('bg-blue-600');
                bar.classList.add('bg-red-500');
                pct.textContent = msg;
                // DO NOT increment uploadedFiles here. 
                // But we should "processQueue" to not stall others.
                // Should we mark "totalFiles" down? 
                // Or denote failure count? For now just keep totals but progress won't reach 100%
                processQueue();
            }
        }

        function checkAllDone() {
            if (activeUploads === 0 && uploadQueue.length === 0) {
                 if (uploadedFiles === totalFiles) {
                    document.getElementById('closeBtn').innerHTML = "All Done - Refresh Page";
                    document.getElementById('closeBtn').classList.add('bg-green-600', 'text-white', 'hover:bg-green-700');
                } else {
                     document.getElementById('closeBtn').innerHTML = "Completed with Errors";
                }
            }
        }
        
        function handleModalClose() {
             // If we did uploads, reload to show them.
             if (successfulUploadsCount > 0) {
                 location.reload();
             } else {
                 document.getElementById('uploadModal').classList.add('hidden');
                 // Reset UI for next time?
                 document.getElementById('uploadUi').classList.remove('hidden');
                 document.getElementById('totalProgress').classList.add('hidden');
                 document.getElementById('progressContainer').classList.add('hidden');
             }
        }
        
        // --- NEW FEATURES ---

        function openRenameModal(key) {
            document.getElementById('renameModal').classList.remove('hidden');
            document.getElementById('renameOldKey').value = key;
            document.getElementById('renameNewKey').value = key; // Pre-fill
        }

        async function openInNewTab(key) {
             try {
                const response = await fetch(`/get_link?key=${encodeURIComponent(key)}`);
                const data = await response.json();
                if (data.url) {
                    window.open(data.url, '_blank');
                } else {
                    showToast('Failed to get link', true);
                }
            } catch (err) {
                showToast('Error opening file', true);
            }
        }

        // --- PREVIEW NAVIGATION ---
        let previewFilesList = [];
        let currentPreviewIndex = -1;
        let currentPreviewKey = null; // Track current key for actions
        let touchStartX = 0;
        let touchEndX = 0;

        document.addEventListener('keydown', (e) => {
            if (document.getElementById('previewModal').classList.contains('hidden')) return;
            
            if (e.key === 'ArrowLeft') showPrevPreview();
            if (e.key === 'ArrowRight') showNextPreview();
            if (e.key === 'Escape') closePreview();
        });
        
        const previewModal = document.getElementById('previewModal');
        previewModal.addEventListener('touchstart', e => {
            touchStartX = e.changedTouches[0].screenX;
        });
        
        previewModal.addEventListener('touchend', e => {
            touchEndX = e.changedTouches[0].screenX;
            handleSwipe();
        });
        
        function handleSwipe() {
            if (touchEndX < touchStartX - 50) showNextPreview();
            if (touchEndX > touchStartX + 50) showPrevPreview();
        }

        function refreshPreviewList() {
            // Collect all viewable files currently in the DOM
            // We look for button onclick="openPreview(...)"
            const btns = document.querySelectorAll('button[onclick^="openPreview"]');
            previewFilesList = [];
            btns.forEach((btn, index) => {
                // Parse the onclick attribute to get key and name
                // onclick="openPreview('key', 'name')"
                const match = btn.getAttribute('onclick').match(/openPreview\('([^']*)', '([^']*)'\)/);
                if (match) {
                    previewFilesList.push({
                        key: match[1],
                        name: match[2],
                        index: index
                    });
                }
            });
        }

        async function openPreview(key, name) {
            refreshPreviewList();
            
            // Find current index
            currentPreviewIndex = previewFilesList.findIndex(f => f.key === key);
            
            await loadPreviewContent(key, name);
            document.getElementById('previewModal').classList.remove('hidden');
        }
        
        function showNextPreview() {
            if (currentPreviewIndex === -1 || previewFilesList.length === 0) return;
            currentPreviewIndex = (currentPreviewIndex + 1) % previewFilesList.length;
            const item = previewFilesList[currentPreviewIndex];
            loadPreviewContent(item.key, item.name);
        }

        function showPrevPreview() {
            if (currentPreviewIndex === -1 || previewFilesList.length === 0) return;
            currentPreviewIndex = (currentPreviewIndex - 1 + previewFilesList.length) % previewFilesList.length;
            const item = previewFilesList[currentPreviewIndex];
            loadPreviewContent(item.key, item.name);
        }
        
        async function loadPreviewContent(key, name) {
            currentPreviewKey = key;
            document.getElementById('previewTitle').textContent = name;
            const content = document.getElementById('previewContent');
            const openBtn = document.getElementById('previewOpenBtn');
            
            content.innerHTML = '<i class="fa-solid fa-circle-notch fa-spin text-4xl text-blue-500"></i>';
            openBtn.href = '#'; // Reset
            
            try {
                // Request SIGNED link for preview (default behavior now)
                const response = await fetch(`/get_link?key=${encodeURIComponent(key)}`);
                const data = await response.json();
                
                if (data.url) {
                    openBtn.href = data.url; // Set open button link
                    
                    const ext = name.split('.').pop().toLowerCase();
                    if (['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg'].includes(ext)) {
                        content.innerHTML = `<img src="${data.url}" class="max-w-full max-h-[70vh] rounded shadow-lg select-none" draggable="false">`;
                    } else if (['mp4', 'webm', 'mov'].includes(ext)) {
                        content.innerHTML = `<video src="${data.url}" controls autoplay class="max-w-full max-h-[70vh] rounded shadow-lg"></video>`;
                    } else if (ext === 'pdf') {
                        content.innerHTML = `<iframe src="${data.url}" class="w-full h-[70vh] rounded border border-slate-200"></iframe>`;
                    } else {
                         content.innerHTML = `
                            <div class="text-center">
                                <i class="fa-regular fa-file text-6xl text-slate-300 mb-4 block"></i>
                                <p class="text-slate-500 mb-4">No preview available.</p>
                                <a href="${data.url}" target="_blank" class="text-blue-600 hover:underline">Download / Open</a>
                            </div>
                        `;
                    }
                } else {
                    content.innerHTML = '<p class="text-red-500">Error loading link.</p>';
                }
            } catch (err) {
                content.innerHTML = '<p class="text-red-500">Error loading preview.</p>';
            }
        }
        
        function closePreview() {
            document.getElementById('previewModal').classList.add('hidden');
            document.getElementById('previewContent').innerHTML = ''; 
            currentPreviewKey = null;
        }

        async function deleteCurrentPreview() {
            if (!currentPreviewKey) return;
            if (!confirm('Are you sure you want to delete this file?')) return;
            
            const formData = new FormData();
            formData.append('key', currentPreviewKey);
            
            try {
                const res = await fetch('/delete', {
                    method: 'POST',
                    body: formData
                });
                
                if (res.ok) {
                    window.location.reload();
                } else {
                    alert('Failed to delete file');
                }
            } catch (e) {
                alert('Error deleting file');
            }
        }

        // Feature: Copy Link
        async function copyLink(key) {
            try {
                // EXPLICITLY ask for public link
                const response = await fetch(`/get_link?key=${encodeURIComponent(key)}&public=true`);
                const data = await response.json();
                
                if (data.url) {
                    await navigator.clipboard.writeText(data.url);
                    showToast('Link copied to clipboard!');
                } else {
                    showToast('Failed to generate link', true);
                }
            } catch (err) {
                console.error(err);
                showToast('Error copying link', true);
            }
        }

        function showToast(message, isError = false) {
            const toast = document.getElementById('toast');
            document.getElementById('toastMessage').textContent = message;
            
            if (isError) {
                toast.classList.add('bg-red-800');
            } else {
                toast.classList.remove('bg-red-800');
            }

            toast.classList.remove('translate-y-20', 'opacity-0');
            
            setTimeout(() => {
                toast.classList.add('translate-y-20', 'opacity-0');
            }, 3000);
        }
    </script>
</body>
</html>
"""

# --- ROUTES ---

# --- ROUTES ---

@app.route('/set_bucket/<name>')
def set_bucket(name):
    if name in PROFILES:
        session['active_profile'] = name
    return redirect(url_for('index'))

@app.route('/')
def index():
    prefix = request.args.get('prefix', '')
    continuation_token = request.args.get('continuation_token', '')
    
    folders, files, next_token, error = list_files(prefix, continuation_token)
    
    if error:
        flash(f'Error accessing S3: {error}', 'error')
    
    return render_template_string(HTML_TEMPLATE, 
                                  folders=folders, 
                                  files=files, 
                                  prefix=prefix, 
                                  next_token=next_token,
                                  bucket_name=get_bucket(),
                                  current_profile_name=get_current_config()['name'])

@app.route('/get_upload_link', methods=['POST'])
def get_upload_link():
    data = request.json
    filename = data.get('filename')
    prefix = data.get('prefix', '')
    
    if not filename:
        return {"error": "Missing filename"}, 400
        
    s3 = get_s3_client()
    key = prefix + filename
    
    print(f"DEBUG: Checking key='{key}'")
    try:
        s3.head_object(Bucket=get_bucket(), Key=key)
        print(f"DEBUG: Found existing key='{key}'")
        return {"error": "File already exists", "exists": True}, 409
    except ClientError as e:
        # Check for 404 or NoSuchKey
        code = e.response.get('Error', {}).get('Code', '')
        if code != '404' and code != 'NoSuchKey':
             print(f"DEBUG: Error checking key='{key}': {e}")
             return {"error": str(e)}, 500
         # If 404, proceed
    
    try:
        # Generate presigned URL for PUT (Direct Upload)
        # ContentType is important for the browser to send it correctly
        content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        
        url = s3.generate_presigned_url(
            'put_object',
            Params={
                'Bucket': get_bucket(), 
                'Key': key,
                'ContentType': content_type
            },
            ExpiresIn=3600
        )
        return {"url": url, "key": key, "content_type": content_type}
    except ClientError as e:
        return {"error": str(e)}, 500

@app.route('/get_link')
def get_link():
    key = request.args.get('key')
    want_public = request.args.get('public') == 'true'
    
    if not key:
        return {"error": "Missing key"}, 400
        
    # Use Public URL ONLY if requested and available
    public_url = get_current_config().get('public_url')
    if want_public and public_url:
        # Ensure trailing slash on public url and no leading slash on key
        base = public_url.rstrip('/')
        clean_key = key.lstrip('/')
        url = f"{base}/{clean_key}"
    else:
        # Default to PRESIGNED URL for everything else (Preview, Open, Download)
        s3 = get_s3_client()
        try:
            url = s3.generate_presigned_url(
                'get_object',
                Params={'Bucket': get_bucket(), 'Key': key},
                ExpiresIn=3600 * 24
            )
        except ClientError as e:
            return {"error": str(e)}, 500

    return {"url": url}

@app.route('/mkdir', methods=['POST'])
def mkdir():
    s3 = get_s3_client()
    prefix = request.form.get('prefix', '')
    folder_name = request.form.get('folder_name', '').strip()
    
    if not folder_name:
        flash('Folder name is required', 'error')
        return redirect(url_for('index', prefix=prefix))
        
    if not folder_name.endswith('/'):
        folder_name += '/'
        
    try:
        key = prefix + folder_name
        s3.put_object(Bucket=get_bucket(), Key=key)
        flash(f'Folder {folder_name} created!', 'success')
    except ClientError as e:
        flash(f'Folder creation failed: {str(e)}', 'error')
        
    return redirect(url_for('index', prefix=prefix))

@app.route('/delete', methods=['POST'])
def delete():
    s3 = get_s3_client()
    key = request.form.get('key')
    prefix = request.form.get('prefix', '')
    
    if not key:
        flash('No key provided for deletion', 'error')
        return redirect(url_for('index', prefix=prefix))
        
    try:
        if key.endswith('/'):
            objects_to_delete = s3.list_objects_v2(Bucket=get_bucket(), Prefix=key)
            if 'Contents' in objects_to_delete:
                delete_keys = [{'Key': obj['Key']} for obj in objects_to_delete['Contents']]
                s3.delete_objects(Bucket=get_bucket(), Delete={'Objects': delete_keys})
        else:
            s3.delete_object(Bucket=get_bucket(), Key=key)
            
        flash(f'Item deleted successfully', 'success')
    except ClientError as e:
        flash(f'Deletion failed: {str(e)}', 'error')
        
    return redirect(url_for('index', prefix=prefix))

@app.route('/download')
def download():
    key = request.args.get('key')
    
    if not key:
        return "Missing key", 400
        
    s3 = get_s3_client()
    try:
        url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': get_bucket(), 'Key': key},
            ExpiresIn=3600
        )
        return redirect(url)
    except ClientError as e:
        return f"Error generating download link: {str(e)}", 500

@app.route('/rename', methods=['POST'])
def rename():
    s3 = get_s3_client()
    old_key = request.form.get('old_key')
    new_key = request.form.get('new_key').strip()
    
    if not old_key or not new_key:
        flash('Missing filenames', 'error')
        return redirect(url_for('index'))
        
    try:
        # Check if identical
        if old_key == new_key:
            flash('New name is same as old name', 'warning')
            return redirect(url_for('index', prefix=os.path.dirname(old_key)))

        # Handle Folder Rename (Prefix)
        if old_key.endswith('/'):
            # Ensure new key also ends with /
            if not new_key.endswith('/'):
                new_key += '/'
                
            # List all objects in the old folder
            objects = s3.list_objects_v2(Bucket=get_bucket(), Prefix=old_key)
            if 'Contents' in objects:
                for obj in objects['Contents']:
                    old_obj_key = obj['Key']
                    # Replace prefix
                    new_obj_key = new_key + old_obj_key[len(old_key):]
                    
                    # Copy
                    copy_source = {'Bucket': get_bucket(), 'Key': old_obj_key}
                    s3.copy_object(CopySource=copy_source, Bucket=get_bucket(), Key=new_obj_key)
                    # Delete
                    s3.delete_object(Bucket=get_bucket(), Key=old_obj_key)
            
            # Put empty folder marker to accept the move
            s3.put_object(Bucket=get_bucket(), Key=new_key)
            
            # Also delete average folder marker if it exists
            s3.delete_object(Bucket=get_bucket(), Key=old_key)

            flash(f'Folder renamed to {new_key}', 'success')
            
        else:
            # File Rename
            copy_source = {'Bucket': get_bucket(), 'Key': old_key}
            s3.copy_object(CopySource=copy_source, Bucket=get_bucket(), Key=new_key)
            s3.delete_object(Bucket=get_bucket(), Key=old_key)
            flash(f'Renamed to {new_key}', 'success')
            
    except ClientError as e:
        flash(f'Rename failed: {str(e)}', 'error')
        
    # Redirect to the folder of the NEW key
    new_prefix = os.path.dirname(new_key.rstrip('/'))
    if new_prefix: new_prefix += '/'
    else: new_prefix = ''
    
    return redirect(url_for('index', prefix=new_prefix))

def configure_cors():
    """Configures CORS to allow direct browser uploads for all configured buckets."""
    configured_buckets = set()
    
    for profile_key, config in PROFILES.items():
        bucket_name = config['bucket_name']
        if bucket_name in configured_buckets:
            continue
            
        try:
            print(f"Configuring CORS for bucket '{bucket_name}' (via {profile_key})...")
            # Create a dedicated client for this setup to avoid session dependency
            s3 = boto3.client(
                's3',
                aws_access_key_id=config['access_key_id'],
                aws_secret_access_key=config['secret_access_key'],
                endpoint_url=config['endpoint_url'],
                region_name=config['region']
            )
            
            cors_configuration = {
                'CORSRules': [{
                    'AllowedHeaders': ['*'],
                    'AllowedMethods': ['GET', 'PUT', 'POST', 'HEAD'],
                    'AllowedOrigins': ['*'],
                    'ExposeHeaders': ['ETag']
                }]
            }
            s3.put_bucket_cors(Bucket=bucket_name, CORSConfiguration=cors_configuration)
            print(f"✅ CORS configuration successfully applied to bucket {bucket_name}.")
            configured_buckets.add(bucket_name)
        except ClientError as e:
            print(f"⚠️ Failed to apply CORS configuration to {bucket_name}: {e}")
        except Exception as e:
            print(f"⚠️ Unexpected error configuring CORS for {bucket_name}: {e}")

if __name__ == '__main__':
    # Apply CORS on startup to ensure direct uploads work
    configure_cors()
    
    app.run(host='0.0.0.0', debug=True, port=5000)
