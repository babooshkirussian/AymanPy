
def fibonacci(n):
    # return a list of fibonacci numbers
    fibseq=[0,1]
    for i in range(n):
        fibseq.append(fibseq[-1]+fibseq[-2])
    print(fibseq)
fibonacci(5)