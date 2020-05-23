import arxiv
import sys
import habanero
import re
import pickle
import os
import unidecode
import argparse
import time
import progressbar
import json
import inspect

try:
    from imbibe.opts import optional_bibtex_fields
except ModuleNotFoundError:
    from imbibe.opts_default import optional_bibtex_fields

try:
    with open("capitalized_words.txt", "r") as f:
        protected_words = [ line.rstrip("\n") for line in f ]
except FileNotFoundError:
    protected_words = []

cr = habanero.Crossref(ua_string = "imbibe")

def unescape_string(s):
    return re.sub(r'(?<!\\)\\', '', s)

def populate_arxiv_information(list_of_bibitems):
    bibitems_with_arxivid = [ b for b in list_of_bibitems if
            (b.arxivid is not None and not b.arxiv_populated) ]
    arxiv_ids = [ b.arxivid for b in bibitems_with_arxivid ]

    if len(arxiv_ids) == 0:
        return

    try:
        results = arxiv.query(id_list=arxiv_ids, max_results=len(arxiv_ids))
    except Exception as e:
        if e.args[0] != 'HTTP Error 400 in query':
            raise e

        # Need to try all the arXiv IDs individually to find out which one was
        # not found.
        results = [ None ] * len(arxiv_ids)
        for i in range(len(results)):
            try:
                results[i] = arxiv.query(id_list=arxiv_ids[i])[0]
            except Exception as ee:
                print("arXiv ID not found (or other error): " + arxiv_ids[i], file=sys.stderr)
                raise ee from None

    if len(results) != len(arxiv_ids):
        raise RuntimeError("arXiv returned wrong number of papers.")

    for bibitem,result in zip(bibitems_with_arxivid, results):
        bibitem.read_arxiv_information(result)

def crossref_read(dois):
    chunk_size = 1
    if len(dois) <= chunk_size:
        results = cr.works(ids=dois)
        if len(dois) == 1:
            results = [ results ]
        return results
    else:
        results = []
        it = range(0,len(dois),chunk_size)

        if len(dois) > 5:
            print("Retrieving Crossref data (might take a while)...", file=sys.stderr)
            it = progressbar.progressbar(it)

        for i in it:
            results += crossref_read(dois[i:(i+chunk_size)])
            time.sleep(0.1)
        return results

def populate_doi_information(list_of_bibitems):
    bibitems_with_doi = [ b for b in list_of_bibitems if (b.doi is not None and
        not b.doi_populated) ]
    dois = [ b.doi for b in bibitems_with_doi ]
    if len(dois) == 0:
        return

    results = crossref_read(dois)

    for bibitem,result in zip(bibitems_with_doi, results):
        bibitem.read_journal_information(result)

def format_author(auth):
    return auth['family'] + ", " + auth['given']

def format_authorlist(l):
    if len(l) == 0:
        return ""
    else:
        return ''.join(s + " and " for s in l[0:-1]) + l[-1]

def strip_nonalphabetic(s):
    return ''.join(c for c in s if c.isalpha())

def make_bibtexid_from_arxivid(firstauthorlastname, arxivid):
    if "/" in arxivid:
        # Old style arxiv id.
        yymm = arxivid.split('/')[1][0:4]
    else:
        # New style arxiv id.
        yymm = arxivid.split(".")[0]
        assert len(yymm) == 4

    firstauthorlastname = unidecode.unidecode(strip_nonalphabetic(firstauthorlastname))
    return firstauthorlastname + "_" + yymm

def process_text(text):
    if isinstance(text, str):
        # Some character substitutions to deal with Unicode characters that LaTeX tends to choke on.
        subs = { "\u2008" : " " ,
                 "\u2212" : "--" }
        def replace(c):
            if c in subs.keys():
                return subs[c]
            else:
                return c

        return "".join(replace(c) for c in text)
    else:
        return text

def protect_words(title):
    split = re.split('(\W)', title)
    print(split, file=sys.stderr)
    for i in range(len(split)):
        word = split[i]
        if len(word) == 0:
            continue
        if word[0].isupper() and word in protected_words:
            split[i] = "{" + word + "}"
    return ''.join(split)

def origcase_heuristic(title):
    words = title.split()
    n = len(words)
    ncapitalized = sum(word[0].isupper() for word in words)
    if ncapitalized / n > 0.5:
        return False
    else:
        return True

class BibItem(object):
    cache = {}
    badjournals = []

    def __init__(self, arxivid=None, doi=None):
        if arxivid is None and doi is None:
            raise ValueError("Need to specify either arXiv ID or DOI!")

        if arxivid is not None:
            self.canonical_id = 'arXiv:' + arxivid
        else:
            self.canonical_id = 'doi:' + doi

        self.arxivid = arxivid
        self.doi = doi
        self.journal = None
        self.detailed_authors = None
        self.bibtex_id = None
        self.abstract = None
        self.comment = None

        self.arxiv_populated = False
        self.doi_populated = False

    def load_bad_journals():
        thisfile = inspect.getfile(inspect.currentframe())
        filename = os.path.join(os.path.dirname(thisfile), "badjournals.txt")
        with open(filename, "r") as f:
            return [ line.rstrip("\n") for line in f ]

    @staticmethod
    def load_cache(filename):
        try:
            with open(filename, 'rb') as f:
                def init_from_dict(d):
                    obj = BibItem.__new__(BibItem)
                    obj.__dict__ = d
                    return obj

                BibItem.cache = dict( (k, init_from_dict(obj)) for k,obj in json.load(f).items())
        except FileNotFoundError:
            print("Warning: cache file not found.", file=sys.stderr)

    @staticmethod
    def save_cache(filename):
        with open(filename, 'w') as f:
            json.dump(dict( (k,i.__dict__) for k,i in BibItem.cache.items()),
                    f, indent=0)

    @staticmethod
    def init_from_input_file_line(line):
        if line in BibItem.cache:
            return BibItem.cache[line]

        splitline = re.split(r'(?<!\\)\[|(?<!\\)\]', line)
        main = splitline[0]
        doi = None
        arxivid = None
        main = main.strip()

        if main[0:4] == 'doi:':
            doi = main[4:]
        else:
            arxivid = main

        bibtex_id = None
        suppress_volumewarning = False

        comment = None
        origcase = None
        extra_bibtex_fields = {}
        if len(splitline) > 1:
            opts = splitline[1]
            for opt in opts.split(","):
                opt = opt.strip()
                opt_split = opt.split(':')
                key = opt_split[0]
                value = opt_split[1]

                if key == 'doi':
                    if doi is not None:
                        raise RuntimeError("Specified DOI twice.")
                    else:
                        doi = value
                elif key == 'bibtex_id':
                    bibtex_id = value
                elif key == 'suppress_volumewarning':
                    if value == 'yes':
                        suppress_volumewarning = True
                    elif value == 'no':
                        pass
                    else:
                        raise RuntimeError("Invalid value: '" + value + "'")
                elif key == 'comment':
                    comment = unescape_string(value)
                elif key == 'origcase':
                    if value == 'yes':
                        origcase = True
                    elif value == 'no':
                        origcase = False
                    elif value == 'auto':
                        origcase = None
                elif key in optional_bibtex_fields:
                    extra_bibtex_fields[key] = unescape_string(value)
                else:
                    raise RuntimeError("Invalid option name: '" + key + "'")

        bibitem = BibItem(arxivid, doi)
        bibitem.bibtex_id = bibtex_id
        bibitem.suppress_volumewarning = suppress_volumewarning
        bibitem.comment = comment
        bibitem.origcase = origcase
        bibitem.extra_bibtex_fields = extra_bibtex_fields

        BibItem.cache[line] = bibitem
        return bibitem

    def generate_bibtexid(self):
        if self.bibtex_id is not None:
            return self.bibtex_id
        elif self.arxivid is None:
            raise ValueError("For papers not referenced by arXiv ID, you have to" +
                              "manually specify a BibTeX ID.")
        else:
            return make_bibtexid_from_arxivid(self.first_author_lastname(), self.arxivid)

    def first_author_lastname(self):
        if self.detailed_authors is not None:
            return self.detailed_authors[0]['family']
        else:
            return self.authors[0].split(' ')[-1]

    def __eq__(a,b):
        return a.canonical_id == b.canonical_id

    def __ne__(a,b):
        return not (a == b)

    def __hash__(self):
        return hash(self.canonical_id)

    def output_bib(self, eprint_published):
        if self.bibtex_id is not None:
            bibtex_id = self.bibtex_id
        else:
            bibtex_id = self.generate_bibtexid()

        try:
            if self.comment is not None:
                print(self.comment)
        except AttributeError:
            pass

        print("@article{" + self.generate_bibtexid() + ",")
        if self.abstract is not None:
            print("  abstract={" + self.abstract + "},")
        if self.arxivid is not None and (self.doi is None or eprint_published):
            print("  archiveprefix={arXiv},")
            print("  eprint={" + self.arxivid + "},")
        if self.journal is not None:
            print("  journal={" + self.journal_short + "},")
            print("  pages={" + self.page + "},")
            print("  year={" + str(self.year) + "},")

            # Sometimes papers don't come with volume numbers for some reason...
            if self.volume is not None:
                print("  volume={" + self.volume + "},")
            elif not self.suppress_volumewarning:
                print("WARNING: No volume in CrossRef data for paper:", file=sys.stderr)
                print("   " + self.title, file=sys.stderr)
        if self.doi is not None:
            print("  doi={" + self.doi + "},")

        try:
            origcase = self.origcase
        except AttributeError:
            origcase = None
        if origcase is None:
            origcase = origcase_heuristic(self.title)
        if origcase:
            print("  title={{" + self.title + "}},")
        else:
            print("  title={" + protect_words(self.title) + "},")

        try:
            extra_bibtex_fields = self.extra_bibtex_fields
        except AttributeError:
            extra_bibtex_fields = {}
        for key,value in extra_bibtex_fields.items():
            print("  " + key + "={" + value + "},")

        print("  author={" + format_authorlist(self.authors) + "}")
        print("}")
        print("")

    def read_arxiv_information(self,arxivresult):
        self.authors = arxivresult['authors']
        self.title = arxivresult['title']
        self.abstract = arxivresult['summary']

        if self.doi is not None and arxivresult['doi'] is not None and self.doi != arxivresult['doi']:
            print("WARNING: manually specified DOI for arXiv:" + self.arxivid + " disagrees with arXiv information.", file=sys.stderr)
            print("You have: ", file=sys.stderr)
            print("arXiv has: " + arxivresult['doi'], file=sys.stderr)
            print("Using your DOI.", file=sys.stderr)
            print(file=sys.stderr)
        elif arxivresult['doi'] is not None:
            self.doi = arxivresult['doi']

        self.arxiv_populated = True

    def bad_journal_exit(self, journalname):
        print("The following journal is known to have improper Crossref data:", file=sys.stderr)
        print("    " + journalname, file=sys.stderr)
        print("You will need to add papers from this journal to your BibTeX file manually.", file=sys.stderr)
        print("Exiting with error.", file=sys.stderr)
        sys.exit(1)

    def bad_type_exit(self, crossref_type):
        print("The Crossref entry with DOI:", file=sys.stderr)
        print("    " + self.doi, file=sys.stderr)
        if self.arxivid is not None:
            print("linked to arXiv ID:", file=sys.stderr)
            print("    " + self.arxivid, file=sys.stderr)
        print("has type:", file=sys.stderr)
        print("    " + crossref_type, file=sys.stderr)
        print("Currently, only type 'journal-article' is supported.", file=sys.stderr)
        print("You will need to add this entry to your BibTeX file manually.", file=sys.stderr)
        print("Exiting with error.", file=sys.stderr)
        sys.exit(1)

    def read_journal_information(self,cr_result):
        try:
            cr_result = cr_result['message']
            crossref_type = cr_result['type']
            if crossref_type != "journal-article":
                self.bad_type_exit(crossref_type)
            self.detailed_authors = cr_result['author']
            self.authors = [ format_author(auth) for auth in self.detailed_authors ]
            self.journal = cr_result['container-title'][0]
            if self.journal in BibItem.badjournals:
                self.bad_journal_exit(self.journal)
            try:
                self.journal_short = cr_result['short-container-title'][0]
            except IndexError:
                self.journal_short = self.journal
            self.year = cr_result['issued']['date-parts'][0][0]
            self.title = cr_result['title'][0]

            try:
                self.volume = cr_result['volume']
            except KeyError:
                self.volume = None

            try:
                self.page = cr_result['article-number']
            except KeyError:
                self.page = cr_result['page'].split('-')[0]

            self.doi_populated = True
        except KeyError:
            print(cr_result)
            raise
BibItem.badjournals = BibItem.load_bad_journals()

class OpenFileWithPath:
    @staticmethod
    def open(path, *args, **kwargs):
        return OpenFileWithPath(path, open(path, *args, **kwargs))

    def __init__(self, path, f):
        self.f = f
        self.path = path

    def close_and_delete(self):
        self.f.close()
        os.remove(self.path)

    def __getattr__(self, attr):
        return getattr(self.f, attr)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(prog='imbibe')
    parser.add_argument("--no-eprint-published", action='store_false',
            dest='eprint_published',
            help="For published papers, don't include the arXiv ID in the BibTeX file.")
    parser.add_argument("--print-keys", action='store_true',
            dest='print_keys',
            help="Instead of outputting BibTeX entries, just output the BibTeX IDs, separated by commas.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--arxiv")
    group.add_argument("--doi")
    group.add_argument("inputfile", nargs='?')
    parser.add_argument("outputfile", nargs='?')
    args = parser.parse_args()

    use_cache=False
    fout=None
    try:
        if args.arxiv is not None:
            bibitems = [ BibItem(arxivid=args.arxiv) ]
        elif args.doi is not None:
            bibitems = [ BibItem(doi=args.doi) ]
            bibitems[0].bibtex_id = 'ARTICLE'
        else:
            use_cache = True
            cache_filename = "imbibe-cache.json"
            BibItem.load_cache(cache_filename)

            if args.outputfile is not None:
                outputfilename = args.outputfile
                fout = OpenFileWithPath.open(outputfilename, 'w', encoding='utf-8')
            else:
                fout = sys.stdout
            print_ = print
            def myprint(*args, file=fout):
                print_(*(process_text(arg) for arg in args), file=file)
            print = myprint

            f = open(args.inputfile)
            bibitems = [ BibItem.init_from_input_file_line(line) for line in f.readlines() 
                    if line.strip() != '' ]

            if not args.print_keys:
                if 'IMBIBE_MSG' in os.environ:
                    msg = os.environ['IMBIBE_MSG']
                else:
                    msg = "File automatically generated by imbibe. DO NOT EDIT."
                print(msg)
                print()

        populate_arxiv_information(bibitems)
        populate_doi_information(bibitems)

        if args.print_keys:
            for bibitem in bibitems:
                print(bibitem.generate_bibtexid(), end=", ")
            print()
        else:
            for bibitem in bibitems:
                bibitem.output_bib(args.eprint_published)

        if use_cache:
            BibItem.save_cache(cache_filename)
    except:
        if fout is not None and isinstance(fout, OpenFileWithPath):
            fout.close_and_delete()
        raise
