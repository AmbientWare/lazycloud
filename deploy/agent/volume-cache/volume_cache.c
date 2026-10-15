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
// too. On 64-bit glibc open is openat on AT_FDCWD with O_LARGEFILE, so the
// open family forwards to the openat family.
#undef _FORTIFY_SOURCE
#undef _FILE_OFFSET_BITS
#undef _TIME_BITS
#define _GNU_SOURCE

#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <sched.h>
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

typedef int (*openat_fn)(int, const char *, int, ...);
typedef int (*openat2_fn)(int, const char *, int);
typedef FILE *(*fopen_fn)(const char *, const char *);

struct real {
	openat_fn openat, openat64;
	openat2_fn openat_2, openat64_2;
	fopen_fn fopen, fopen64;
};

struct config {
	bool debug;
	char cache[PATH_MAX];
	size_t cache_len;
	char volumes[VOLUME_LIST_MAX];
	size_t volume_count;
	const char *volume[MAX_VOLUMES];
	size_t volume_len[MAX_VOLUMES];
};

// dlsym binds to the oldest version glibc exports, so an image with glibc
// 2.17 or newer loads the library; a newer binding would have ld.so refuse
// the preload on older images and every exec print an error.
#if defined(__x86_64__)
__asm__(".symver dlsym,dlsym@GLIBC_2.2.5");
#elif defined(__aarch64__)
__asm__(".symver dlsym,dlsym@GLIBC_2.17");
#endif

static struct real real;
static bool real_resolved;
// config_state is 0 before load_config runs, 1 while a thread runs it and
// 2 once it is loaded.
static int config_state;
static struct config config;

// Set while dlsym runs. dlsym may allocate, and an allocator's first call
// may open a file (its own tuning in /proc or /sys); such an open goes to
// the kernel directly, as the real functions are not known yet. No
// allocator opens through fopen, so fopen refuses the window with EAGAIN.
// Initial-exec TLS keeps the flag's access free of __tls_get_addr, whose
// first call on a thread may itself allocate.
static __thread bool resolving __attribute__((tls_model("initial-exec")));

// Threads that race here store the same pointers; the atomic stores and
// REAL's loads keep that well defined.
static void resolve(void)
{
	if (__atomic_load_n(&real_resolved, __ATOMIC_ACQUIRE))
		return;
	resolving = true;
#define RESOLVE(f, type, name) __atomic_store_n(&real.f, (type)dlsym(RTLD_NEXT, name), __ATOMIC_RELAXED)
	RESOLVE(openat, openat_fn, "openat");
	RESOLVE(openat64, openat_fn, "openat64");
	RESOLVE(openat_2, openat2_fn, "__openat_2");
	RESOLVE(openat64_2, openat2_fn, "__openat64_2");
	RESOLVE(fopen, fopen_fn, "fopen");
	RESOLVE(fopen64, fopen_fn, "fopen64");
#undef RESOLVE
	resolving = false;
	__atomic_store_n(&real_resolved, true, __ATOMIC_RELEASE);
}

#define REAL(f) __atomic_load_n(&real.f, __ATOMIC_RELAXED)

static void debug_line(const char *decision, const char *path, const char *reason)
{
	if (!config.debug)
		return;
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

static void load_config_once(void);

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
				debug_line("disabled", cursor, "(too many volume paths)");
				config.volume_count = 0;
				return;
			}
			config.volume[config.volume_count] = cursor;
			config.volume_len[config.volume_count] = len;
			config.volume_count++;
		}
		cursor = sep;
	}
}

// load_config_once runs load_config in the first thread that calls it and
// has the others wait for it; a benign race here would tear the strings.
static void load_config_once(void)
{
	int state = 0;
	if (__atomic_load_n(&config_state, __ATOMIC_ACQUIRE) == 2)
		return;
	if (__atomic_compare_exchange_n(&config_state, &state, 1, false, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
		load_config();
		__atomic_store_n(&config_state, 2, __ATOMIC_RELEASE);
		return;
	}
	while (__atomic_load_n(&config_state, __ATOMIC_ACQUIRE) != 2)
		sched_yield();
}

static bool under_volume(const char *path)
{
	if (path[0] != '/')
		return false;
	bool under = false;
	for (size_t i = 0; i < config.volume_count && !under; i++) {
		size_t len = config.volume_len[i];
		under = len == 1 || (strncmp(path, config.volume[i], len) == 0 && (path[len] == '\0' || path[len] == '/'));
	}
	if (!under)
		return false;
	// A ".." component could name a file the cache directory does not mirror.
	for (const char *p = path; (p = strstr(p, "/..")) != NULL; p += 3)
		if (p[3] == '\0' || p[3] == '/')
			return false;
	return true;
}

// cache_copy writes the cache path of path into cached and returns it when
// the open may be redirected there: a read-only open, relative to no
// directory, of a file under a volume whose cache copy is a regular file
// of the same size and mtime second as the mount's. NULL sends the open to
// the mount.
static const char *cache_copy(int dirfd, const char *path, int flags, char *cached)
{
	if (dirfd != AT_FDCWD || path == NULL)
		return NULL;
	load_config_once();
	if (config.volume_count == 0 || (flags & O_ACCMODE) != O_RDONLY || (flags & REFUSED_FLAGS) != 0 || !under_volume(path))
		return NULL;
	size_t path_len = strlen(path);
	if (config.cache_len + path_len >= PATH_MAX) {
		debug_line("mount", path, "(path too long)");
		return NULL;
	}
	memcpy(cached, config.cache, config.cache_len);
	memcpy(cached + config.cache_len, path, path_len + 1);

	struct stat mount_st, cache_st;
	const char *reason = NULL;
	if (syscall(SYS_newfstatat, AT_FDCWD, path, &mount_st, 0) != 0)
		reason = "(mount stat failed)";
	else if (syscall(SYS_newfstatat, AT_FDCWD, cached, &cache_st, 0) != 0)
		reason = "(no cache copy)";
	else if (!S_ISREG(mount_st.st_mode) || !S_ISREG(cache_st.st_mode))
		reason = "(not a regular file)";
	else if (mount_st.st_size != cache_st.st_size)
		reason = "(size differs)";
	else if (mount_st.st_mtim.tv_sec != cache_st.st_mtim.tv_sec)
		reason = "(mtime differs)";
	if (reason == NULL)
		return cached;
	debug_line("mount", path, reason);
	return NULL;
}

static int raw_open(int dirfd, const char *path, int flags, mode_t mode)
{
	return (int)syscall(SYS_openat, dirfd, path, flags | O_LARGEFILE, mode);
}

static int openat_common(openat_fn *real_openat, int dirfd, const char *path, int flags, mode_t mode)
{
	if (resolving)
		return raw_open(dirfd, path, flags, mode);
	resolve();
	openat_fn fn = __atomic_load_n(real_openat, __ATOMIC_RELAXED);
	int saved_errno = errno;
	char buf[PATH_MAX];
	const char *cached = cache_copy(dirfd, path, flags, buf);
	if (cached != NULL) {
		int fd = fn(AT_FDCWD, cached, flags);
		debug_line(fd >= 0 ? "cache" : "mount", path, fd >= 0 ? "" : "(cache open failed)");
		if (fd >= 0)
			return fd;
	}
	errno = saved_errno;
	return fn(dirfd, path, flags, mode);
}

static int openat2_common(openat2_fn *real_openat, int dirfd, const char *path, int flags)
{
	if (resolving)
		return raw_open(dirfd, path, flags, 0);
	resolve();
	openat2_fn fn = __atomic_load_n(real_openat, __ATOMIC_RELAXED);
	int saved_errno = errno;
	char buf[PATH_MAX];
	const char *cached = cache_copy(dirfd, path, flags, buf);
	if (cached != NULL) {
		int fd = fn(AT_FDCWD, cached, flags);
		debug_line(fd >= 0 ? "cache" : "mount", path, fd >= 0 ? "" : "(cache open failed)");
		if (fd >= 0)
			return fd;
	}
	errno = saved_errno;
	return fn(dirfd, path, flags);
}

static FILE *fopen_common(fopen_fn *real_fopen, const char *path, const char *mode)
{
	if (resolving) {
		errno = EAGAIN;
		return NULL;
	}
	resolve();
	fopen_fn fn = __atomic_load_n(real_fopen, __ATOMIC_RELAXED);
	int saved_errno = errno;
	char buf[PATH_MAX];
	const char *cached = NULL;
	if (mode != NULL && mode[0] == 'r' && strchr(mode, '+') == NULL)
		cached = cache_copy(AT_FDCWD, path, O_RDONLY, buf);
	if (cached != NULL) {
		FILE *f = fn(cached, mode);
		debug_line(f != NULL ? "cache" : "mount", path, f != NULL ? "" : "(cache open failed)");
		if (f != NULL)
			return f;
	}
	errno = saved_errno;
	return fn(path, mode);
}

static mode_t mode_arg(int flags, va_list ap)
{
	if ((flags & (O_CREAT | O_TMPFILE)) == 0)
		return 0;
	return (mode_t)va_arg(ap, int);
}

EXPORT int open(const char *path, int flags, ...)
{
	va_list ap;
	va_start(ap, flags);
	int fd = openat_common(&real.openat, AT_FDCWD, path, flags, mode_arg(flags, ap));
	va_end(ap);
	return fd;
}

EXPORT int open64(const char *path, int flags, ...)
{
	va_list ap;
	va_start(ap, flags);
	int fd = openat_common(&real.openat64, AT_FDCWD, path, flags, mode_arg(flags, ap));
	va_end(ap);
	return fd;
}

EXPORT int __open_2(const char *path, int flags)
{
	return openat2_common(&real.openat_2, AT_FDCWD, path, flags);
}

EXPORT int __open64_2(const char *path, int flags)
{
	return openat2_common(&real.openat64_2, AT_FDCWD, path, flags);
}

EXPORT int openat(int dirfd, const char *path, int flags, ...)
{
	va_list ap;
	va_start(ap, flags);
	int fd = openat_common(&real.openat, dirfd, path, flags, mode_arg(flags, ap));
	va_end(ap);
	return fd;
}

EXPORT int openat64(int dirfd, const char *path, int flags, ...)
{
	va_list ap;
	va_start(ap, flags);
	int fd = openat_common(&real.openat64, dirfd, path, flags, mode_arg(flags, ap));
	va_end(ap);
	return fd;
}

EXPORT int __openat_2(int dirfd, const char *path, int flags)
{
	return openat2_common(&real.openat_2, dirfd, path, flags);
}

EXPORT int __openat64_2(int dirfd, const char *path, int flags)
{
	return openat2_common(&real.openat64_2, dirfd, path, flags);
}

EXPORT FILE *fopen(const char *path, const char *mode)
{
	return fopen_common(&real.fopen, path, mode);
}

EXPORT FILE *fopen64(const char *path, const char *mode)
{
	return fopen_common(&real.fopen64, path, mode);
}
