Simple test where source: file[1-3] is imported onto a vendor tracking branch.
file1 is the only file modified.

cvs2svn should include trunk as a possible parent for unchanged imported files. The 
alternative is that, if there are more unchanged imported files than changed files
on trunk, the chosen parent will be the most recent cross-repository branch.

http://cvs2svn.tigris.org/issues/show_bug.cgi?id=54

1) Import 3 files into the repository:

2) Modify file1 on trunk

3) Tag trunk TRUNK_TAG_1

4) Branch trunk BRANCH_1

5) Modify file 1 on trunk

6) Tag trunk TRUNK_TAG_2

7) cvs import new upstream sources; file 1 modified

8) merge changes from new import

9) Tag trunk TRUNK_TAG_3

10) Branch trunk BRANCH_2
