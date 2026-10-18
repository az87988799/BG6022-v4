/* Development probe: real collective and point-to-point MPI, no ORCA. */
#include <mpi.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static void json_string(const char *value, int length)
{
    putchar('"');
    for (int i = 0; i < length; ++i) {
        unsigned char ch = (unsigned char)value[i];
        if (ch == '"' || ch == '\\') {
            putchar('\\');
            putchar(ch);
        } else if (ch < 0x20) {
            printf("\\u%04x", (unsigned int)ch);
        } else {
            putchar(ch);
        }
    }
    putchar('"');
}

static void require_mpi(int code, const char *operation, int rank)
{
    if (code != MPI_SUCCESS) {
        fprintf(stderr,
                "{\"event\":\"mpi_error\",\"rank\":%d,\"operation\":\"%s\",\"code\":%d}\n",
                rank, operation, code);
        fflush(stderr);
        MPI_Abort(MPI_COMM_WORLD, code);
        exit(EXIT_FAILURE);
    }
}

int main(int argc, char **argv)
{
    int rank = -1;
    int size = 0;
    setvbuf(stdout, NULL, _IOLBF, 0);
    printf("{\"event\":\"before_mpi_init\",\"pid\":%ld,\"ppid\":%ld,\"uid\":%ld}\n",
           (long)getpid(), (long)getppid(), (long)getuid());
    int init_code = MPI_Init(&argc, &argv);
    if (init_code != MPI_SUCCESS) {
        fprintf(stderr, "{\"event\":\"mpi_init_failed\",\"code\":%d}\n", init_code);
        return EXIT_FAILURE;
    }
    require_mpi(MPI_Comm_set_errhandler(MPI_COMM_WORLD, MPI_ERRORS_RETURN), "set_errhandler", rank);
    require_mpi(MPI_Comm_rank(MPI_COMM_WORLD, &rank), "comm_rank", rank);
    require_mpi(MPI_Comm_size(MPI_COMM_WORLD, &size), "comm_size", rank);

    char version[MPI_MAX_LIBRARY_VERSION_STRING];
    int version_length = 0;
    require_mpi(MPI_Get_library_version(version, &version_length), "library_version", rank);
    if (version_length < 0 || version_length >= (int)sizeof(version)) {
        MPI_Abort(MPI_COMM_WORLD, EXIT_FAILURE);
        return EXIT_FAILURE;
    }
    version[version_length] = '\0';
    int local_failure = (size != 4 || getuid() != 10001 || geteuid() != 10001
                         || strstr(version, "Open MPI v4.1.8") == NULL);
    for (int reporter = 0; reporter < size; ++reporter) {
        require_mpi(MPI_Barrier(MPI_COMM_WORLD), "identity_barrier", rank);
        if (rank == reporter) {
            printf("{\"event\":\"rank_initialized\",\"rank\":%d,\"size\":%d,\"pid\":%ld,\"ppid\":%ld,\"uid\":%ld,\"version\":",
                   rank, size, (long)getpid(), (long)getppid(), (long)getuid());
            json_string(version, version_length);
            puts("}");
        }
    }

    int value = rank + 1;
    int sum = 0;
    require_mpi(MPI_Allreduce(&value, &sum, 1, MPI_INT, MPI_SUM, MPI_COMM_WORLD), "allreduce_sum", rank);
    local_failure |= (sum != 10);

    int received = -1;
    int previous = (rank + size - 1) % size;
    int next = (rank + 1) % size;
    MPI_Status receive_status;
    require_mpi(MPI_Sendrecv(&rank, 1, MPI_INT, next, 418,
                            &received, 1, MPI_INT, previous, 418,
                            MPI_COMM_WORLD, &receive_status), "sendrecv_ring", rank);
    int received_count = -1;
    require_mpi(MPI_Get_count(&receive_status, MPI_INT, &received_count), "receive_count", rank);
    local_failure |= (received != previous || received_count != 1
                      || receive_status.MPI_SOURCE != previous || receive_status.MPI_TAG != 418);

    int failing_ranks = 0;
    require_mpi(MPI_Allreduce(&local_failure, &failing_ranks, 1, MPI_INT, MPI_SUM,
                             MPI_COMM_WORLD), "allreduce_failures", rank);
    for (int reporter = 0; reporter < size; ++reporter) {
        require_mpi(MPI_Barrier(MPI_COMM_WORLD), "result_barrier", rank);
        if (rank == reporter) {
            printf("{\"event\":\"rank_checks\",\"rank\":%d,\"allreduce_sum\":%d,\"ring_received\":%d,\"ring_expected\":%d,\"local_failure\":%d,\"failing_ranks\":%d}\n",
                   rank, sum, received, previous, local_failure, failing_ranks);
        }
    }
    int finalize_code = MPI_Finalize();
    int exit_code = (failing_ranks == 0 && finalize_code == MPI_SUCCESS) ? EXIT_SUCCESS : EXIT_FAILURE;
    printf("{\"event\":\"rank_finalized\",\"rank\":%d,\"pid\":%ld,\"finalize_code\":%d,\"exit_code\":%d}\n",
           rank, (long)getpid(), finalize_code, exit_code);
    return exit_code;
}
