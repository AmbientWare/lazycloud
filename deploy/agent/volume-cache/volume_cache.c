// volume-cache.so is preloaded into workload containers. A read-only open of
// a file under a volume mount opens the cached whole-file copy instead when
// the cache holds the mount's current version, so a warm load reads from the
// disk rather than through FUSE. Any doubt sends the open to the mount.
//
// LAZYCLOUD_VOLUME_CACHE is the directory that mirrors the mount paths: the
// copy of /models/a/b.bin is $LAZYCLOUD_VOLUME_CACHE/models/a/b.bin.
// LAZYCLOUD_VOLUME_PATHS lists the mount paths, colon separated, absolute,
// without trailing slash. With either unset or empty every call passes
// through. LAZYCLOUD_VOLUME_CACHE_DEBUG set writes one line per decision to
// stderr.
//
// A redirect requires the mount and cache paths to both be regular files
// with equal st_size and st_mtim.tv_sec; the cache is a projection of the
// mount and the mount stays the authority for everything else.
//
// The compiler's default _FORTIFY_SOURCE declares open and openat as inline
// wrappers, which would collide with the definitions below. Callers built
// with fortify reach __open_2 and __openat_2 (and their 64 variants) for
// two-argument opens with a non-constant flag word, so those are interposed
// too.
#undef _FORTIFY_SOURCE
#undef _FILE_OFFSET_BITS
#undef _TIME_BITS
#define _GNU_SOURCE

#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#define EXPORT __attribute__((visibility("default")))

// A redirect is only for plain reads. O_PATH and O_DIRECTORY descriptors
// expose the opened path itself; O_NOFOLLOW and O_EXCL change what the
// mount path resolves to; the rest write.
#define REFUSED_FLAGS \
	(O_WRONLY | O_RDWR | O_CREAT | O_TRUNC | O_APPEND | O_EXCL | O_PATH | O_TMPFILE | O_DIRECTORY | O_NOFOLLOW)

#define MAX_VOLUMES 64
#define VOLUME_LIST_MAX 4096

typedef int (*open_fn)(const char *, int, ...);
typedef int (*open2_fn)(const char *, int);
typedef int (*openat_fn)(int, const char *, int, ...);
typedef int (*openat2_fn)(int, const char *, int);
typedef FILE *(*fopen_fn)(const char *, const char *);

struct real {
	open_fn open, open64;
	open2_fn open_2, open64_2;
	openat_fn openat, openat64;
	openat2_fn openat_2, openat64_2;
	fopen_fn fopen, fopen64;
};

struct config {
	bool enabled;
	bool debug;
	char cache[PATH_MAX];
	size_t cache_len;
	char volumes[VOLUME_LIST_MAX];
	size_t volume_count;
	const char *volume[MAX_VOLUMES];
	size_t volume_len[MAX_VOLUMES];
};

static struct real real;
static bool real_resolved;
static pthread_once_t config_once = PTHREAD_ONCE_INIT;
static struct config config;

// Set while dlsym runs. dlsym may allocate, and an allocator's first call
// may open a file (its own tuning in /proc or /sys); such an open goes to
// the kernel directly, as the real functions are not known yet. No
// allocator opens through fopen, so fopen refuses the window with EAGAIN.
// Initial-exec TLS keeps the flag's access free of __tls_get_addr, whose
// first call on a thread may itself allocate.
static __thread bool resolving __attribute__((tls_model("initial-exec")));

static void resolve(void)
{
	if (__atomic_load_n(&real_resolved, __ATOMIC_ACQUIRE))
		return;
	resolving = true;
	struct real r = {
		.open = (open_fn)dlsym(RTLD_NEXT, "open"),
		.open64 = (open_fn)dlsym(RTLD_NEXT, "open64"),
		.open_2 = (open2_fn)dlsym(RTLD_NEXT, "__open_2"),
		.open64_2 = (open2_fn)dlsym(RTLD_NEXT, "__open64_2"),
		.openat = (openat_fn)dlsym(RTLD_NEXT, "openat"),
		.openat64 = (openat_fn)dlsym(RTLD_NEXT, "openat64"),
		.openat_2 = (openat2_fn)dlsym(RTLD_NEXT, "__openat_2"),
		.openat64_2 = (openat2_fn)dlsym(RTLD_NEXT, "__openat64_2"),
		.fopen = (fopen_fn)dlsym(RTLD_NEXT, "fopen"),
		.fopen64 = (fopen_fn)dlsym(RTLD_NEXT, "fopen64"),
	};
	resolving = false;
	// Threads that race here store the same pointers; the atomic stores and
	// REAL's loads keep that well defined.
#define STORE(f) __atomic_store_n(&real.f, r.f, __ATOMIC_RELAXED)
	STORE(open);
	STORE(open64);
	STORE(open_2);
	STORE(open64_2);
	STORE(openat);
	STORE(openat64);
	STORE(openat_2);
	STORE(openat64_2);
	STORE(fopen);
	STORE(fopen64);
#undef STORE
	__atomic_store_n(&real_resolved, true, __ATOMIC_RELEASE);
}

#define REAL(f) __atomic_load_n(&real.f, __ATOMIC_RELAXED)

static void debug_line(const char *decision, const char *path, const char *reason)
{
	char line[PATH_MAX + 64];
	int n = snprintf(line, sizeof line, "volume-cache: %s %s%s%s\n", decision, path,
	                 reason[0] ? " " : "", reason);
	if (n <= 0)
		return;
	if ((size_t)n >= sizeof line)
		n = sizeof line - 1;
	ssize_t written = write(STDERR_FILENO, line, (size_t)n);
	(void)written;
}

static void load_config(void)
{
	config.debug = getenv("LAZYCLOUD_VOLUME_CACHE_DEBUG") != NULL;
	const char *cache = getenv("LAZYCLOUD_VOLUME_CACHE");
	const char *paths = getenv("LAZYCLOUD_VOLUME_PATHS");
	if (cache == NULL || cache[0] != '/' || paths == NULL || paths[0] == '\0')
		return;
	size_t cache_len = strlen(cache);
	while (cache_len > 1 && cache[cache_len - 1] == '/')
		cache_len--;
	if (cache_len >= sizeof config.cache || strlen(paths) >= sizeof config.volumes) {
		if (config.debug)
			debug_line("disabled", cache, "(settings too long)");
		return;
	}
	memcpy(config.cache, cache, cache_len);
	config.cache[cache_len] = '\0';
	config.cache_len = cache_len;
	memcpy(config.volumes, paths, strlen(paths) + 1);
	char *cursor = config.volumes;
	while (cursor != NULL) {
		char *sep = strchr(cursor, ':');
		if (sep != NULL)
			*sep++ = '\0';
		size_t len = strlen(cursor);
		while (len > 1 && cursor[len - 1] == '/')
			cursor[--len] = '\0';
		if (len > 0 && cursor[0] == '/') {
			if (config.volume_count == MAX_VOLUMES) {
				if (config.debug)
					debug_line("disabled", cursor, "(too many volume paths)");
				return;
			}
			config.volume[config.volume_count] = cursor;
			config.volume_len[config.volume_count] = len;
			config.volume_count++;
		}
		cursor = sep;
	}
	config.enabled = config.volume_count > 0;
}

static bool under_volume(const char *path)
{
	if (path[0] != '/')
		return false;
	bool under = false;
	for (size_t i = 0; i < config.volume_count && !under; i++) {
		size_t len = config.volume_len[i];
		if (len == 1)
			under = true;
		else if (strncmp(path, config.volume[i], len) == 0 && (path[len] == '\0' || path[len] == '/'))
			under = true;
	}
	if (!under)
		return false;
	// A ".." component could name a file the cache directory does not mirror.
	for (const char *p = path; (p = strstr(p, "/..")) != NULL; p += 3)
		if (p[3] == '\0' || p[3] == '/')
			return false;
	return true;
}

// Regular files of the same size and mtime second on both sides.
static bool cache_current(const char *path, char *cached, size_t cached_size)
{
	size_t path_len = strlen(path);
	if (config.cache_len + path_len >= cached_size) {
		if (config.debug)
			debug_line("mount", path, "(path too long)");
		return false;
	}
	memcpy(cached, config.cache, config.cache_len);
	memcpy(cached + config.cache_len, path, path_len + 1);

	struct stat mount_st, cache_st;
	if (syscall(SYS_newfstatat, AT_FDCWD, path, &mount_st, 0) != 0) {
		if (config.debug)
			debug_line("mount", path, "(mount stat failed)");
		return false;
	}
	if (syscall(SYS_newfstatat, AT_FDCWD, cached, &cache_st, 0) != 0) {
		if (config.debug)
			debug_line("mount", path, "(no cache copy)");
		return false;
	}
	if (!S_ISREG(mount_st.st_mode) || !S_ISREG(cache_st.st_mode)) {
		if (config.debug)
			debug_line("mount", path, "(not a regular file)");
		return false;
	}
	if (mount_st.st_size != cache_st.st_size) {
		if (config.debug)
			debug_line("mount", path, "(size differs)");
		return false;
	}
	if (mount_st.st_mtim.tv_sec != cache_st.st_mtim.tv_sec) {
		if (config.debug)
			debug_line("mount", path, "(mtime differs)");
		return false;
	}
	return true;
}

static bool candidate(const char *path, int flags)
{
	if (path == NULL)
		return false;
	pthread_once(&config_once, load_config);
	return config.enabled && (flags & O_ACCMODE) == O_RDONLY && (flags & REFUSED_FLAGS) == 0 &&
	       under_volume(path);
}

static bool fopen_candidate(const char *path, const char *mode)
{
	if (path == NULL || mode == NULL || mode[0] != 'r' || strchr(mode, '+') != NULL)
		return false;
	pthread_once(&config_once, load_config);
	return config.enabled && under_volume(path);
}

static int open_common(open_fn real_open, const char *path, int flags, mode_t mode)
{
	int saved_errno = errno;
	char cached[PATH_MAX];
	if (candidate(path, flags) && cache_current(path, cached, sizeof cached)) {
		errno = saved_errno;
		int fd = real_open(cached, flags);
		if (fd >= 0) {
			if (config.debug)
				debug_line("cache", path, "");
			return fd;
		}
		if (config.debug)
			debug_line("mount", path, "(cache open failed)");
	}
	errno = saved_errno;
	return real_open(path, flags, mode);
}

static int open2_common(open2_fn real_open, const char *path, int flags)
{
	int saved_errno = errno;
	char cached[PATH_MAX];
	if (candidate(path, flags) && cache_current(path, cached, sizeof cached)) {
		errno = saved_errno;
		int fd = real_open(cached, flags);
		if (fd >= 0) {
			if (config.debug)
				debug_line("cache", path, "");
			return fd;
		}
		if (config.debug)
			debug_line("mount", path, "(cache open failed)");
	}
	errno = saved_errno;
	return real_open(path, flags);
}

static int openat_common(openat_fn real_openat, int dirfd, const char *path, int flags, mode_t mode)
{
	int saved_errno = errno;
	char cached[PATH_MAX];
	if (dirfd == AT_FDCWD && candidate(path, flags) && cache_current(path, cached, sizeof cached)) {
		errno = saved_errno;
		int fd = real_openat(AT_FDCWD, cached, flags);
		if (fd >= 0) {
			if (config.debug)
				debug_line("cache", path, "");
			return fd;
		}
		if (config.debug)
			debug_line("mount", path, "(cache open failed)");
	}
	errno = saved_errno;
	return real_openat(dirfd, path, flags, mode);
}

static int openat2_common(openat2_fn real_openat, int dirfd, const char *path, int flags)
{
	int saved_errno = errno;
	char cached[PATH_MAX];
	if (dirfd == AT_FDCWD && candidate(path, flags) && cache_current(path, cached, sizeof cached)) {
		errno = saved_errno;
		int fd = real_openat(AT_FDCWD, cached, flags);
		if (fd >= 0) {
			if (config.debug)
				debug_line("cache", path, "");
			return fd;
		}
		if (config.debug)
			debug_line("mount", path, "(cache open failed)");
	}
	errno = saved_errno;
	return real_openat(dirfd, path, flags);
}

static FILE *fopen_common(fopen_fn real_fopen, const char *path, const char *mode)
{
	int saved_errno = errno;
	char cached[PATH_MAX];
	if (fopen_candidate(path, mode) && cache_current(path, cached, sizeof cached)) {
		errno = saved_errno;
		FILE *f = real_fopen(cached, mode);
		if (f != NULL) {
			if (config.debug)
				debug_line("cache", path, "");
			return f;
		}
		if (config.debug)
			debug_line("mount", path, "(cache open failed)");
	}
	errno = saved_errno;
	return real_fopen(path, mode);
}

static mode_t mode_arg(int flags, va_list ap)
{
	if ((flags & (O_CREAT | O_TMPFILE)) == 0)
		return 0;
	return (mode_t)va_arg(ap, int);
}

static int raw_open(int dirfd, const char *path, int flags, mode_t mode)
{
	return (int)syscall(SYS_openat, dirfd, path, flags, mode);
}

EXPORT int open(const char *path, int flags, ...)
{
	va_list ap;
	va_start(ap, flags);
	mode_t mode = mode_arg(flags, ap);
	va_end(ap);
	if (resolving)
		return raw_open(AT_FDCWD, path, flags, mode);
	resolve();
	return open_common(REAL(open), path, flags, mode);
}

EXPORT int open64(const char *path, int flags, ...)
{
	va_list ap;
	va_start(ap, flags);
	mode_t mode = mode_arg(flags, ap);
	va_end(ap);
	if (resolving)
		return raw_open(AT_FDCWD, path, flags | O_LARGEFILE, mode);
	resolve();
	return open_common(REAL(open64), path, flags, mode);
}

EXPORT int __open_2(const char *path, int flags)
{
	if (resolving)
		return raw_open(AT_FDCWD, path, flags, 0);
	resolve();
	return open2_common(REAL(open_2), path, flags);
}

EXPORT int __open64_2(const char *path, int flags)
{
	if (resolving)
		return raw_open(AT_FDCWD, path, flags | O_LARGEFILE, 0);
	resolve();
	return open2_common(REAL(open64_2), path, flags);
}

EXPORT int openat(int dirfd, const char *path, int flags, ...)
{
	va_list ap;
	va_start(ap, flags);
	mode_t mode = mode_arg(flags, ap);
	va_end(ap);
	if (resolving)
		return raw_open(dirfd, path, flags, mode);
	resolve();
	return openat_common(REAL(openat), dirfd, path, flags, mode);
}

EXPORT int openat64(int dirfd, const char *path, int flags, ...)
{
	va_list ap;
	va_start(ap, flags);
	mode_t mode = mode_arg(flags, ap);
	va_end(ap);
	if (resolving)
		return raw_open(dirfd, path, flags | O_LARGEFILE, mode);
	resolve();
	return openat_common(REAL(openat64), dirfd, path, flags, mode);
}

EXPORT int __openat_2(int dirfd, const char *path, int flags)
{
	if (resolving)
		return raw_open(dirfd, path, flags, 0);
	resolve();
	return openat2_common(REAL(openat_2), dirfd, path, flags);
}

EXPORT int __openat64_2(int dirfd, const char *path, int flags)
{
	if (resolving)
		return raw_open(dirfd, path, flags | O_LARGEFILE, 0);
	resolve();
	return openat2_common(REAL(openat64_2), dirfd, path, flags);
}

EXPORT FILE *fopen(const char *path, const char *mode)
{
	if (resolving) {
		errno = EAGAIN;
		return NULL;
	}
	resolve();
	return fopen_common(REAL(fopen), path, mode);
}

EXPORT FILE *fopen64(const char *path, const char *mode)
{
	if (resolving) {
		errno = EAGAIN;
		return NULL;
	}
	resolve();
	return fopen_common(REAL(fopen64), path, mode);
}
